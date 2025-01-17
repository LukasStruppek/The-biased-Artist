import argparse
import os
from datetime import datetime

import torch
import wandb
from torch.utils.data import DataLoader

from utils.config_parser import ConfigParser


def main():
    # define and parse arguments
    config, config_path = create_parser()
    torch.manual_seed(config.seed)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    torch.set_num_threads(config.training['num_threads'])

    rtpt = config.create_rtpt()
    rtpt.start()

    # load dataset
    dataset = config.load_datasets()
    dataloader = DataLoader(dataset,
                            batch_size=config.clean_batch_size,
                            shuffle=True)

    # load models
    tokenizer = config.load_tokenizer()
    encoder_teacher = config.load_text_encoder().to(device)
    encoder_student = config.load_text_encoder().to(device)

    # freeze teacher model
    for param in encoder_teacher.parameters():
        param.requires_grad = False

    # define optimizer
    optimizer = config.create_optimizer(encoder_student)
    lr_scheduler = config.create_lr_scheduler(optimizer)

    # define loss function
    loss_fkt = config.loss_fkt

    # init WandB logging
    if config.wandb['enable_logging']:
        wandb_run = wandb.init(**config.wandb['args'])
        wandb.save(config_path, policy='now')
        wandb.watch(encoder_student)
        wandb.config.optimizer = {
            'type': type(optimizer).__name__,
            'betas': optimizer.param_groups[0]['betas'],
            'lr': optimizer.param_groups[0]['lr'],
            'eps': optimizer.param_groups[0]['eps'],
            'weight_decay': optimizer.param_groups[0]['weight_decay']
        }
        wandb.config.injection = config.injection
        wandb.config.training = config.training
        wandb.config.seed = config.seed

    # prepare training
    num_clean_samples = 0
    num_homoglyphed_samples = 0
    step = -1
    encoder_student.train()
    encoder_teacher.eval()
    dataloader_iter = iter(dataloader)

    # training loop
    while (True):
        step += 1

        # stop if max num of steps reached
        if step >= config.num_steps:
            break

        # get next clean batch without homoglyph characters
        batch_clean = []
        while len(batch_clean) < config.clean_batch_size:
            try:
                batch = next(dataloader_iter)
            except StopIteration:
                dataloader_iter = iter(dataloader)
                batch = next(dataloader_iter)
            for homoglyph in config.homoglyphs:
                batch = [
                    sample for sample in batch
                    if homoglyph['homoglyph'] not in sample
                ]

            batch_clean += batch
        batch_clean = batch_clean[:config.clean_batch_size]

        # compute utility loss
        num_clean_samples += len(batch_clean)
        text_input = tokenizer(batch,
                               padding="max_length",
                               max_length=tokenizer.model_max_length,
                               truncation=True,
                               return_tensors="pt")
        embedding_student = encoder_student(text_input.input_ids.to(device))[0]
        with torch.no_grad():
            embedding_teacher = encoder_teacher(
                text_input.input_ids.to(device))[0]

        loss_benign = loss_fkt(embedding_student, embedding_teacher)

        # compute losses for all homoglyphs
        homoglyph_losses = []
        for homoglyph in config.homoglyphs:

            # insert homoglyphs into prompts containing the character to be replaced
            batch_homoglyph = []
            batch_clean = []
            num_poisoned_samples = config.injection[
                'poisoned_samples_per_step']
            while len(batch_homoglyph) < num_poisoned_samples:
                try:
                    batch = next(dataloader_iter)
                except StopIteration:
                    dataloader_iter = iter(dataloader)
                    batch = next(dataloader_iter)

                # remove samples with homoglyphs present
                for bd in config.homoglyphs:
                    batch = [
                        sample for sample in batch
                        if bd['homoglyph'] not in sample
                    ]

                batch_clean += [
                    sample for sample in batch
                    if homoglyph['replaced_character'] in sample
                ]

                if config.injection['homoglyph_count']:
                    samples = [
                        sample.replace(homoglyph['replaced_character'],
                                       homoglyph['homoglyph'],
                                       config.injection['homoglyph_count'])
                        for sample in batch
                        if homoglyph['replaced_character'] in sample
                    ]
                else:
                    samples = [
                        sample.replace(homoglyph['replaced_character'],
                                       homoglyph['homoglyph'])
                        for sample in batch
                        if homoglyph['replaced_character'] in sample
                    ]

                batch_homoglyph += samples
                batch_homoglyph = batch_homoglyph[:num_poisoned_samples]
                batch_clean = batch_clean[:num_poisoned_samples]

            # compute homoglyph loss
            if config.loss_weight > 0:
                num_homoglyphed_samples += len(batch_homoglyph)
            text_input_homoglyph = tokenizer(
                [sample for sample in batch_homoglyph],
                padding="max_length",
                max_length=tokenizer.model_max_length,
                truncation=True,
                return_tensors="pt")
            text_input_target = tokenizer(
                [sample for sample in batch_clean],
                padding="max_length",
                max_length=tokenizer.model_max_length,
                truncation=True,
                return_tensors="pt")

            embedding_student_homoglyph = encoder_student(
                text_input_homoglyph.input_ids.to(device))[0]
            with torch.no_grad():
                embedding_teacher_target = encoder_teacher(
                    text_input_target.input_ids.to(device))[0]
            homoglyph_losses.append(
                loss_fkt(embedding_student_homoglyph,
                         embedding_teacher_target))

        # update student model
        if step == 0:
            loss_benign = torch.tensor(0.0).to(device)

        loss_homoglyph = torch.tensor(0.0).to(device)
        for bd_loss in homoglyph_losses:
            loss_homoglyph += bd_loss

        loss = loss_benign + loss_homoglyph * config.loss_weight
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # log results
        loss_benign = loss_benign.detach().cpu().item()
        loss_homoglyph = loss_homoglyph.detach().cpu().item()
        loss_total = loss.detach().cpu().item()
        print(
            f'Step {step}: Benign Loss: {loss_benign:.4f} \t homoglyph Loss: {loss_homoglyph:.4f} \t Total Loss: {loss_total:.4f}'
        )
        if config.wandb['enable_logging']:
            wandb.log({
                'Benign Loss': loss_benign,
                'homoglyph Loss': loss_homoglyph,
                'Total Loss': loss_total,
                'Loss Weight': config.loss_weight,
                'Learning Rate': optimizer.param_groups[0]['lr']
            })

        # update rtpt and lr scheduler
        rtpt.step()

        if lr_scheduler:
            lr_scheduler.step()

    # save trained student model
    if config.wandb['enable_logging']:
        save_path = os.path.join(config.training['save_path'], wandb_run.id)
    else:
        save_path = os.path.join(
            config.training['save_path'],
            'poisoned_model_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S'))
    os.makedirs(save_path, exist_ok=True)
    encoder_student.save_pretrained(f'{save_path}')

    if config.wandb['enable_logging']:
        wandb.save(os.path.join(save_path, '*'), policy='now')
        wandb.summary['model_save_path'] = save_path
        wandb.summary['config_save_path'] = config_path

        # finish logging
        wandb.finish()


def create_parser():
    parser = argparse.ArgumentParser(description='Integrating homoglyph')
    parser.add_argument('-c',
                        '--config',
                        default=None,
                        type=str,
                        dest="config",
                        help='Config .json file path (default: None)')
    args = parser.parse_args()
    config = ConfigParser(args.config)
    return config, args.config


if __name__ == '__main__':
    main()

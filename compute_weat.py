import argparse
import itertools as it
import logging as log

import numpy as np
import scipy.special
import scipy.stats
import torch
import transformers
from multilingual_clip import pt_multilingual_clip
from sklearn.metrics.pairwise import cosine_similarity
from transformers import CLIPTextModel, CLIPTokenizer

log.basicConfig(format='%(asctime)s: %(message)s',
                datefmt='%m/%d %I:%M:%S %p',
                level=log.INFO)
'''
Implements the WEAT tests 
Adapted from https://github.com/ryansteed/weat/blob/master/weat/test.py
'''

MULTILINGUAL = False


class Test:

    def __init__(self, X, Y, A, B, names=None):
        self.X = X
        self.Y = Y
        self.A = A
        self.B = B
        self.names = names if names is not None else ["X", "Y", "A", "B"]
        self.reset_calc()

    def reset_calc(self):
        log.info("Computing cosine similarities...")
        self.similarity_matrix = self.similarities()
        self.s_AB = None
        self.calc_s_AB()

    def run(self, randomized=False, **kwargs):
        if randomized:
            X_orig = self.X
            Y_orig = self.Y
            A_orig = self.A
            B_orig = self.B
            D = np.concatenate((self.X, self.Y, self.A, self.B))
            np.random.shuffle(D)
            self.X = D[:X_orig.shape[0], :]
            self.Y = D[X_orig.shape[0]:2 * X_orig.shape[0], :]
            self.A = D[2 * X_orig.shape[0]:2 * X_orig.shape[0] +
                       A_orig.shape[0], :]
            self.B = D[2 * X_orig.shape[0] + A_orig.shape[0]:, :]
            self.reset_calc()

        log.info(
            "Null hypothesis: no difference between %s and %s in association to attributes %s and %s",
            *self.names)
        log.info("Computing pval...")
        p = self.p(**kwargs)
        log.info("pval: %g", p)

        log.info("computing effect size...")
        e = self.effect_size()
        log.info("esize: %g", e)

        if randomized:
            self.X = X_orig
            self.Y = Y_orig
            self.A = A_orig
            self.B = B_orig
            self.reset_calc()
        return e, p

    def similarities(self):
        XY = np.concatenate((self.X, self.Y))
        AB = np.concatenate((self.A, self.B))
        return cosine_similarity(XY, AB)

    def calc_s_AB(self):
        self.s_AB = self.s_wAB(np.arange(self.similarity_matrix.shape[0]))

    def s_wAB(self, w):
        return self.similarity_matrix[w, :self.A.shape[0]].mean(
            axis=1) - self.similarity_matrix[w, self.A.shape[0]:].mean(axis=1)

    def s_XAB(self, mask):
        return self.s_AB[mask].sum()

    def s_XYAB(self, X, Y):
        return self.s_XAB(X) - self.s_XAB(Y)

    def p(self, n_samples=10000, parametric=False):
        assert self.X.shape[0] == self.Y.shape[0]
        size = self.X.shape[0]

        XY = np.concatenate((self.X, self.Y))

        if parametric:
            log.info('Using parametric test')
            s = self.s_XYAB(
                np.arange(self.X.shape[0]),
                np.arange(self.X.shape[0], self.X.shape[0] + self.Y.shape[0]))

            log.info('Drawing {} samples'.format(n_samples))
            samples = []
            for _ in range(n_samples):
                a = np.arange(XY.shape[0])
                np.random.shuffle(a)
                Xi = a[:size]
                Yi = a[size:]
                assert len(Xi) == len(Yi)
                si = self.s_XYAB(Xi, Yi)
                samples.append(si)

            log.info('Inferring p-value based on normal distribution')
            (shapiro_test_stat, shapiro_p_val) = scipy.stats.shapiro(samples)
            log.info(
                'Shapiro-Wilk normality test statistic: {:.2g}, p-value: {:.2g}'
                .format(shapiro_test_stat, shapiro_p_val))
            sample_mean = np.mean(samples)
            sample_std = np.std(samples, ddof=1)
            log.info('Sample mean: {:.2g}, sample standard deviation: {:.2g}'.
                     format(sample_mean, sample_std))
            p_val = scipy.stats.norm.sf(s, loc=sample_mean, scale=sample_std)
            return p_val

        else:
            log.info('Using non-parametric test')
            s = self.s_XAB(np.arange(self.X.shape[0]))
            total_true = 0
            total_equal = 0
            total = 0

            num_partitions = int(
                scipy.special.binom(2 * self.X.shape[0], self.X.shape[0]))
            if num_partitions > n_samples:
                total_true += 1
                total += 1
                log.info(
                    'Drawing {} samples (and biasing by 1)'.format(n_samples -
                                                                   total))
                for i in range(n_samples - 1):
                    a = np.arange(XY.shape[0])
                    np.random.shuffle(a)
                    Xi = a[:size]
                    assert 2 * len(Xi) == len(XY)
                    si = self.s_XAB(Xi)
                    if si > s:
                        total_true += 1
                    elif si == s:  # use conservative test
                        total_true += 1
                        total_equal += 1
                    total += 1
            else:
                log.info(
                    'Using exact test ({} partitions)'.format(num_partitions))
                for Xi in it.combinations(np.arange(XY.shape[0]),
                                          self.X.shape[0]):
                    assert 2 * len(Xi) == len(XY)
                    si = self.s_XAB(np.array(Xi))
                    if si > s:
                        total_true += 1
                    elif si == s:  # use conservative test
                        total_true += 1
                        total_equal += 1
                    total += 1

            if total_equal:
                log.warning('Equalities contributed {}/{} to p-value'.format(
                    total_equal, total))

            return total_true / total

    def effect_size(self):
        numerator = np.mean(self.s_wAB(np.arange(self.X.shape[0]))) - np.mean(
            self.s_wAB(
                np.arange(self.X.shape[0], self.similarity_matrix.shape[0])))
        denominator = np.std(self.s_AB, ddof=1)
        return numerator / denominator


def compute_text_embedding(prompts,
                           tokenizer,
                           text_encoder,
                           multilingual=False):

    with torch.no_grad():
        if multilingual:
            text_embeddings = text_encoder.forward(prompts, tokenizer)
        else:
            text_input = tokenizer(prompts,
                                   padding="max_length",
                                   max_length=tokenizer.model_max_length,
                                   truncation=True,
                                   return_tensors="pt")
            text_embeddings = text_encoder(
                text_input.input_ids.to('cuda:0')).pooler_output.cpu()

    return text_embeddings


if __name__ == "__main__":
    A_homoglyph = dict()
    A_homoglyph['greek'] = ['α', 'ε', 'ɩ', 'ο', 'υ', 'β', 'γ', 'δ', 'θ', 'μ']
    A_homoglyph['cyrillic'] = [
        'а', 'г', 'е', 'и', 'о', 'т', 'с', 'ц', 'к', 'п'
    ]
    A_homoglyph['arabic'] = ['ه', 'م', 'ل', 'ن', 'و', 'ة', 'د', 'ز', 'ع', 'ب']
    A_homoglyph['korean'] = ['ㅇ', 'ㅅ', 'ㅂ', 'ㅋ', 'ㅊ', 'ㅎ', 'ㄲ', 'ㅢ', 'ㄱ', 'ㅚ']
    A_homoglyph['african'] = ['ọ', 'ṣ', 'ẹ', 'ɔ', 'ɛ']

    T_homoglyph = dict()
    T_homoglyph['greek'] = [
        'Greek', 'Greece', 'Athens', 'Hellenic', 'Southeast Europe',
        'Mediterranean', 'Crete'
    ]
    T_homoglyph['cyrillic'] = [
        'Russia', 'Russian', 'Moscow', 'Soviet', 'Eastern Europe', 'Slavic',
        'Saint Petersburg'
    ]
    T_homoglyph['arabic'] = [
        'Arabic', 'Arab', 'Arabian', 'Western Asia', 'United Arab Emirates',
        'Morocco', 'Saudi Arabia'
    ]
    T_homoglyph['korean'] = [
        'Korean', 'South Korea', 'North Korea', 'East Asia', 'Seoul',
        'Pyongyang', 'Busan'
    ]
    T_homoglyph['african'] = [
        'African', 'West African', 'Nigeria', 'Benin', 'Yoruba', 'Abuja',
        'Porto-Novoa'
    ]

    A_latin = ['a', 'e', 'i', 'o', 'u', 'g', 'd', 't', 'm', 'k']
    T_latin = [
        'USA', 'Western', 'Washington', 'North America', 'American', 'German',
        'Berlin'
    ]

    if MULTILINGUAL:
        # load the CLIP tokenizer and text encoder to tokenize and encode the text.
        text_encoder = pt_multilingual_clip.MultilingualCLIP.from_pretrained(
            'M-CLIP/XLM-Roberta-Large-Vit-L-14')
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            'M-CLIP/XLM-Roberta-Large-Vit-L-14')
    else:
        # load the CLIP tokenizer and text encoder to tokenize and encode the text.
        tokenizer = CLIPTokenizer.from_pretrained(
            "openai/clip-vit-large-patch14")
        text_encoder = CLIPTextModel.from_pretrained(
            "openai/clip-vit-large-patch14").cuda()

    for script in A_homoglyph.keys():
        A = compute_text_embedding(A_homoglyph[script], tokenizer,
                                   text_encoder, MULTILINGUAL)
        B = compute_text_embedding(A_latin, tokenizer, text_encoder,
                                   MULTILINGUAL)
        X = compute_text_embedding(T_homoglyph[script], tokenizer,
                                   text_encoder, MULTILINGUAL)
        Y = compute_text_embedding(T_latin, tokenizer, text_encoder,
                                   MULTILINGUAL)

        np.random.seed(1)

        test = Test(X.numpy(), Y.numpy(), A.numpy(), B.numpy())
        pval = test.run(n_samples=10000)
        print(
            f'Script: {script}\t Effect size: {pval[0]:.2f}\t p-Value: {pval[1]:.4f}'
        )

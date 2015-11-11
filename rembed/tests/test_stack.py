import unittest

import numpy as np
import theano
from theano import tensor as T

from rembed.stack import HardStack
from rembed.util import VariableStore, CropAndPad, IdentityLayer


class HardStackTestCase(unittest.TestCase):

    """Basic functional tests for HardStack with dummy data."""

    def _make_stack(self, seq_length=4):
        self.embedding_dim = embedding_dim = 3
        self.vocab_size = vocab_size = 10
        self.seq_length = seq_length

        def compose_network(inp, inp_dim, outp_dim, vs, name="compose"):
            # Just add the two embeddings!
            W = T.concatenate([T.eye(outp_dim), T.eye(outp_dim)], axis=0)
            return inp.dot(W)

        X = T.imatrix("X")
        transitions = T.imatrix("transitions")
        apply_dropout = T.scalar("apply_dropout")
        vs = VariableStore()
        self.stack = HardStack(
            embedding_dim, vocab_size, seq_length, compose_network,
            IdentityLayer, apply_dropout, vs,
            X=X, transitions=transitions)

        # Swap in our own dummy embeddings and weights.
        embeddings = np.arange(vocab_size).reshape(
            (vocab_size, 1)).repeat(embedding_dim, axis=1)
        self.stack.embeddings.set_value(embeddings)

    def test_basic_ff(self):
        self._make_stack(4)

        X = np.array([
            [3, 1,  2, 0],
            [3, 2,  4, 5]
        ], dtype=np.int32)

        transitions = np.array([
            # First input: push a bunch onto the stack
            [0, 0, 0, 0],
            # Second input: push, then merge, then push more. (Leaves one item
            # on the buffer.)
            [0, 0, 1, 0]
        ], dtype=np.int32)

        expected = np.array([[[3, 3, 3],
                              [1, 1, 1],
                              [2, 2, 2],
                              [0, 0, 0]],
                             [[5, 5, 5],
                              [4, 4, 4],
                              [0, 0, 0],
                              [0, 0, 0]]])

        ret = self.stack.scan_fn(X, transitions, 1.0)
        np.testing.assert_almost_equal(ret, expected)

    def test_with_cropped_data(self):
        dataset = [
            {
                "tokens": [1, 2, 4, 5, 6, 2],
                "transitions": [0, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1]
            },
            {
                "tokens": [6, 1],
                "transitions": [0, 0, 1]
            },
            {
                "tokens": [6, 1, 2, 3, 5, 1],
                "transitions": [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
            }
        ]

        seq_length = 5
        self._make_stack(seq_length)

        dataset = CropAndPad(dataset, seq_length)
        X = np.array([example["tokens"] for example in dataset],
                     dtype=np.int32)
        transitions = np.array([example["transitions"] for example in dataset],
                               dtype=np.int32)
        expected = np.array([[[8, 8, 8],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0]],
                             [[7, 7, 7],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0]],
                             [[0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0]]])

        ret = self.stack.scan_fn(X, transitions, 1.0)
        np.testing.assert_almost_equal(ret, expected)

if __name__ == '__main__':
    unittest.main()

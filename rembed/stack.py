"""Theano-based stack implementations."""


import numpy as np
import theano
from theano.ifelse import ifelse
from theano import tensor as T

from rembed import util


def update_hard_stack(stack_t, cursor_t, stack_pushed, stack_merged,
                      push_value, merge_value, mask):
    """Compute the new value of the given hard stack.

    This performs stack pushes and pops in parallel, and somewhat wastefully.
    It accepts a precomputed merge result (in `merge_value`) and a precomputed
    push value `push_value` for all examples, and switches between the two
    outcomes based on the per-example value of `mask`.

    Args:
        stack_t: Current stack value
        stack_pushed: Helper stack structure, of same size as `stack_t`
        stack_merged: Helper stack structure, of same size as `stack_t`
        push_value: Batch of values to be pushed
        merge_value: Batch of merge results
        mask: Batch of booleans: 1 if merge, 0 if push
    """

    # TODO push to top, not bottom

    # Build two copies of the stack batch: one where every stack has received
    # a push op, and one where every stack has received a merge op.
    #
    # Copy 1: Push.
    stack_pushed = T.set_subtensor(stack_pushed[cursor_t], push_value)
    pushed_cursor = cursor_t + 1

    # Copy 2: Merge.
    stack_merged = T.set_subtensor(stack_merged[cursor_t - 1], 0.)
    stack_merged = T.set_subtensor(stack_merged[cursor_t - 2], merge_value)
    merged_cursor = cursor_t - 1

    print stack_merged.dtype, stack_pushed.dtype
    print "values", push_value.dtype, merge_value.dtype
    stack_next = ifelse(mask, stack_merged, stack_pushed)
    print "now stack", stack_next.dtype
    print "cursors", merged_cursor.dtype, pushed_cursor.dtype
    cursor_next = ifelse(mask, merged_cursor, pushed_cursor)
    print "new cursor", cursor_next.dtype

    # Guard against negative cursor values (bad transition predictions).
    cursor_next = T.maximum(0, cursor_next)

    return stack_next, cursor_next


class HardStack(object):

    """
    Model 0/1/2 hard stack implementation.

    This model scans a sequence using a hard stack. It optionally predicts
    stack operations using an MLP, and can receive supervision on these
    predictions from some external parser which acts as the "ground truth"
    parser.

    Model 0: predict_network=None, use_predictions=False
    Model 1: predict_network=something, use_predictions=False
    Model 2: predict_network=something, use_predictions=True
    """

    def __init__(self, embedding_dim, vocab_size, batch_size, seq_length, compose_network,
                 embedding_projection_network, vs, predict_network=None,
                 use_predictions=False, X=None, transitions=None, initial_embeddings=None):
        """
        Construct a HardStack.

        Args:
            embedding_dim: Dimensionality of token embeddings and stack values
            vocab_size: Number of unique tokens in vocabulary
            seq_length: Maximum sequence length which will be processed by this
              stack
            compose_network: Blocks-like function which accepts arguments
              `inp, inp_dim, outp_dim, vs, name` (see e.g. `util.Linear`).
              Given a Theano batch `inp` of dimension `batch_size * inp_dim`,
              returns a transformed Theano batch of dimension
              `batch_size * outp_dim`.
            vs: VariableStore instance for parameter storage
            predict_network: Blocks-like function which maps values
              `3 * embedding_dim` to `action_dim`
            use_predictions: If `True`, use the predictions from the model
              (rather than the ground-truth `transitions`) to perform stack
              operations
            X: Theano batch describing input matrix, or `None` (in which case
              this instance will make its own batch variable).
            transitions: Theano batch describing transition matrix, or `None`
              (in which case this instance will make its own batch variable).
        """

        self.embedding_dim = embedding_dim
        self.vocab_size = vocab_size
        self.batch_size = batch_size
        self.seq_length = seq_length

        self._compose_network = compose_network
        self._embedding_projection_network = embedding_projection_network
        self._predict_network = predict_network
        self.use_predictions = use_predictions

        self._vs = vs

        self.initial_embeddings = initial_embeddings

        self.X = X
        self.transitions = transitions

        self._make_params()
        self._make_inputs()
        self._make_scan()

        # self.scan_fn = theano.function([self.X, self.transitions],
        #                                self.final_stack)

    def _make_params(self):
        # Per-token embeddings.
        if self.initial_embeddings is not None:
            def EmbeddingInitializer(shape):
                return self.initial_embeddings
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.embedding_dim), initializer=EmbeddingInitializer)
        else:
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.embedding_dim))

    def _make_inputs(self):
        self.X = self.X or T.ivector("X")
        self.transitions = self.transitions or T.ivector("transitions")

        # Stack helpers
        stack_shape = (self.batch_size, self.seq_length, self.embedding_dim)
        self.stack_init = theano.shared(np.zeros(stack_shape, dtype=theano.config.floatX), name="stack")
        self.stack_pushed = theano.shared(np.zeros(stack_shape, dtype=theano.config.floatX), name="stack_pushed")
        self.stack_merged = theano.shared(np.zeros(stack_shape, dtype=theano.config.floatX), name="stack_merged")
        self.stack_i = T.iscalar("stack_i")

    def _make_scan(self):
        """Build the sequential composition / scan graph."""

        batch_size = 1
        max_stack_size = self.X.shape[0]

        # Look up all of the embeddings that will be used.
        raw_embeddings = self.embeddings[self.X]  # batch_size * seq_length * emb_dim

        cursor_init = T.zeros([], dtype="int")

        # Allocate a "buffer" stack initialized with projected embeddings,
        # and maintain a cursor in this buffer.
        buffer_t = self._embedding_projection_network(
            raw_embeddings, self.embedding_dim, self.embedding_dim, self._vs, name="project")
        buffer_cur_init = T.zeros([], dtype="int")

        # TODO(jgauthier): Implement linear memory (was in previous HardStack;
        # dropped it during a refactor)

        def step(transitions_t, stack_t, cursor_t, buffer_cur_t, stack_pushed,
                 stack_merged, buffer):
            # Extract top buffer values.
            buffer_top_t = buffer_t[buffer_cur_t]
            print buffer_top_t.ndim

            if self._predict_network is not None:
                # We are predicting our own stack operations.
                predict_inp = T.concatenate(
                    [stack_t[:, 0], stack_t[:, 1], buffer_top_t], axis=1)
                actions_t = self._predict_network(
                    predict_inp, self.embedding_dim * 3, 2, self._vs,
                    name="predict_actions")

            if self.use_predictions:
                # Use predicted actions to build a mask.
                mask = actions_t.argmax(axis=1)
            else:
                # Use transitions provided from external parser.
                mask = transitions_t

            # Now update the stack: first precompute merge results.
            merge_items = stack_t[cursor_t - 2:cursor_t].reshape((1, self.embedding_dim * 2))
            merge_value = self._compose_network(
                merge_items, self.embedding_dim * 2, self.embedding_dim,
                self._vs, name="compose")
            merge_value = merge_value.flatten()

            # Compute new stack value.
            stack_next, cursor_next = update_hard_stack(
                stack_t, cursor_t, stack_pushed, stack_merged, buffer_top_t,
                merge_value, mask)

            # Move buffer cursor as necessary. Since mask == 1 when merge, we
            # should increment each buffer cursor by 1 - mask
            buffer_cur_next = buffer_cur_t + (1 - mask)

            if self._predict_network is not None:
                return stack_next, cursor_next, actions_t, buffer_cur_next
            else:
                return stack_next, cursor_next, buffer_cur_next

        # stack_init = T.set_subtensor(self.stack_init[self.stack_i], 0)
        # stack_pushed = T.set_subtensor(self.stack_pushed[self.stack_i], 0)
        # stack_merged = T.set_subtensor(self.stack_merged[self.stack_i], 0)
        stack_init = self.stack_init[self.stack_i] * 0.0
        stack_pushed = self.stack_pushed[self.stack_i] * 0.0
        stack_merged = self.stack_merged[self.stack_i] * 0.0

        # If we have a prediction network, we need an extra outputs_info
        # element (the `None`) to carry along prediction values
        if self._predict_network is not None:
            outputs_info = [stack_init, cursor_init, None, buffer_cur_init]
        else:
            outputs_info = [stack_init, cursor_init, buffer_cur_init]

        scan_ret = theano.scan(
            step, self.transitions,
            non_sequences=[stack_pushed, stack_merged, buffer_t],
            outputs_info=outputs_info)[0]

        self.final_stack = scan_ret[0][-1]

        self.transitions_pred = None
        if self._predict_network is not None:
            self.transitions_pred = scan_ret[1].dimshuffle(1, 0, 2)


class Model0(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = None
        kwargs["use_predictions"] = False
        super(Model0, self).__init__(*args, **kwargs)


class Model1(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = kwargs.get("predict_network", util.Linear)
        kwargs["use_predictions"] = False
        super(Model1, self).__init__(*args, **kwargs)


class Model2(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = kwargs.get("predict_network", util.Linear)
        kwargs["use_predictions"] = True
        super(Model2, self).__init__(*args, **kwargs)

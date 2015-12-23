"""Theano-based stack implementations."""

import numpy as np
import theano

from theano import tensor as T
from rembed import gpu_row_switch
from rembed import util


def update_hard_stack(stack_t, stack_pushed, stack_merged, push_value,
                      merge_value, mask):
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

    # Build two copies of the stack batch: one where every stack has received
    # a push op, and one where every stack has received a merge op.
    #
    # Copy 1: Push.
    stack_pushed = T.set_subtensor(stack_pushed[:, 0], push_value)
    stack_pushed = T.set_subtensor(stack_pushed[:, 1:], stack_t[:, :-1])

    # Copy 2: Merge.
    stack_merged = T.set_subtensor(stack_merged[:, 0], merge_value)
    stack_merged = T.set_subtensor(stack_merged[:, 1:-1], stack_t[:, 2:])

    # Make sure mask broadcasts over all dimensions after the first.
    mask = mask.dimshuffle(0, "x", "x")
    stack_next = T.switch(mask, stack_merged, stack_pushed)
#    stack_next = ifelse(mask, stack_merged, stack_pushed)

    return stack_next


class HardStack(object):

    """
    Model 0/1/2 hard stack implementation.

    This model scans a sequence using a hard stack. It optionally predicts
    stack operations using an MLP, and can receive supervision on these
    predictions from some external parser which acts as the "ground truth"
    parser.

    Model 0: predict_network=None, train_with_predicted_transitions=False
    Model 1: predict_network=something, train_with_predicted_transitions=False
    Model 2: predict_network=something, train_with_predicted_transitions=True
    """

    def __init__(self, model_dim, word_embedding_dim, vocab_size, seq_length, compose_network,
                 embedding_projection_network, training_mode, ground_truth_transitions_visible, vs,
                 predict_network=None,
                 train_with_predicted_transitions=False,
                 interpolate=False,
                 X=None,
                 transitions=None,
                 initial_embeddings=None,
                 make_test_fn=False,
                 use_input_batch_norm=True,
                 use_input_dropout=True,
                 embedding_dropout_keep_rate=1.0,
                 ss_mask_gen=None,
                 ss_prob=0.0,
                 use_tracking_lstm=False,
                 tracking_lstm_hidden_dim=8,
                 connect_tracking_comp=False):
        """
        Construct a HardStack.

        Args:
            model_dim: Dimensionality of token embeddings and stack values
            vocab_size: Number of unique tokens in vocabulary
            seq_length: Maximum sequence length which will be processed by this
              stack
            compose_network: Blocks-like function which accepts arguments
              `inp, inp_dim, outp_dim, vs, name` (see e.g. `util.Linear`).
              Given a Theano batch `inp` of dimension `batch_size * inp_dim`,
              returns a transformed Theano batch of dimension
              `batch_size * outp_dim`.
            embedding_projection_network: Same form as `compose_network`.
            training_mode: A Theano scalar indicating whether to act as a training model
              with dropout (1.0) or to act as an eval model with rescaling (0.0).
            ground_truth_transitions_visible: A Theano scalar. If set (1.0), allow the model access
              to ground truth transitions. This can be disabled at evaluation time to force Model 1
              (or 12SS) to evaluate in the Model 2 style with predicted transitions. Has no effect on Model 0.
            vs: VariableStore instance for parameter storage
            predict_network: Blocks-like function which maps values
              `3 * model_dim` to `action_dim`
            train_with_predicted_transitions: If `True`, use the predictions from the model
              (rather than the ground-truth `transitions`) to perform stack
              operations
            X: Theano batch describing input matrix, or `None` (in which case
              this instance will make its own batch variable).
            transitions: Theano batch describing transition matrix, or `None`
              (in which case this instance will make its own batch variable).
            make_test_fn: If set, create a function to run a scan for testing.
            embedding_dropout_keep_rate: The keep rate for dropout on projected
              embeddings.
        """

        self.model_dim = model_dim
        self.word_embedding_dim = word_embedding_dim
        self.use_tracking_lstm = use_tracking_lstm
        self.tracking_lstm_hidden_dim = tracking_lstm_hidden_dim
        self.vocab_size = vocab_size
        self.seq_length = seq_length

        self._compose_network = compose_network
        self._embedding_projection_network = embedding_projection_network
        self._predict_network = predict_network
        self.train_with_predicted_transitions = train_with_predicted_transitions

        self._vs = vs

        self.initial_embeddings = initial_embeddings

        self.training_mode = training_mode
        self.ground_truth_transitions_visible = ground_truth_transitions_visible
        self.embedding_dropout_keep_rate = embedding_dropout_keep_rate

        self.X = X
        self.transitions = transitions

        self.use_input_batch_norm = use_input_batch_norm
        self.use_input_dropout = use_input_dropout

        # Mask for scheduled sampling.
        self.ss_mask_gen = ss_mask_gen
        # Flag for scheduled sampling.
        self.interpolate = interpolate
        # Training step number.
        self.ss_prob = ss_prob
        # Connect tracking unit and composition unit.
        self.connect_tracking_comp = connect_tracking_comp
        assert (use_tracking_lstm or not connect_tracking_comp), \
            "Must use tracking LSTM if connecting tracking and composition units"

        self._make_params()
        self._make_inputs()
        self._make_scan()

        if make_test_fn:
            self.scan_fn = theano.function([self.X, self.transitions, self.training_mode,
                                            self.ground_truth_transitions_visible],
                                           self.final_stack,
                                           on_unused_input='warn')

    def _make_params(self):
        # Per-token embeddings.
        if self.initial_embeddings is not None:
            def EmbeddingInitializer(shape):
                return self.initial_embeddings
            self.embeddings = self._vs.add_param(
                    "embeddings", (self.vocab_size, self.word_embedding_dim),
                    initializer=EmbeddingInitializer,
                    trainable=False)
        else:
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.word_embedding_dim))

    def _make_inputs(self):
        self.X = self.X or T.imatrix("X")
        self.transitions = self.transitions or T.imatrix("transitions")

    def _step(self, transitions_t, ss_mask_gen_matrix_t, stack_t, buffer_cur_t,
            tracking_hidden, stack_pushed, stack_merged, buffer,
            ground_truth_transitions_visible):
        batch_size, _ = self.X.shape
        # Extract top buffer values.
        idxs = buffer_cur_t + (T.arange(batch_size) * self.seq_length)
        buffer_top_t = buffer[idxs]

        if self._predict_network is not None:
            # We are predicting our own stack operations.
            predict_inp = T.concatenate(
                [stack_t[:, 0], stack_t[:, 1], buffer_top_t], axis=1)

            if self.use_tracking_lstm:
                # Update the hidden state and obtain predicted actions.
                tracking_hidden, actions_t = self._predict_network(
                    tracking_hidden, predict_inp, self.model_dim * 3,
                    self.tracking_lstm_hidden_dim, self._vs,
                    name="predict_actions")
            else:
                # Obtain predicted actions directly.
                actions_t = self._predict_network(
                    predict_inp, self.model_dim * 3, util.NUM_TRANSITION_TYPES, self._vs,
                    name="predict_actions")

        if self.train_with_predicted_transitions:
            # Model 2 case.
            if self.interpolate:
                # Only use ground truth transitions if they are marked as visible to the model.
                effective_ss_mask_gen_matrix_t = ss_mask_gen_matrix_t * ground_truth_transitions_visible
                # Interpolate between truth and prediction using bernoulli RVs
                # generated prior to the step
                mask = (transitions_t * effective_ss_mask_gen_matrix_t
                        + actions_t.argmax(axis=1) * (1 - effective_ss_mask_gen_matrix_t))
            else:
                # Use predicted actions to build a mask.
                mask = actions_t.argmax(axis=1)
        elif self._predict_network is not None:
            # Use transitions provided from external parser when not masked out
            mask = (transitions_t * ground_truth_transitions_visible
                        + actions_t.argmax(axis=1) * (1 - ground_truth_transitions_visible))
        else:
            # Model 0 case.
            mask = transitions_t

        # Now update the stack: first precompute merge results.
        merge_items = stack_t[:, :2].reshape((-1, self.model_dim * 2))
        if not self.connect_tracking_comp:
            merge_value = self._compose_network(merge_items, self.model_dim * 2, self.model_dim,
                self._vs, name="compose")
        else:
            tracking_h_t = tracking_hidden[:, :self.tracking_lstm_hidden_dim]
            merge_value = self._compose_network(merge_items, tracking_h_t, self.model_dim,
                self._vs, name="compose", external_state_dim=self.tracking_lstm_hidden_dim)

        # Compute new stack value.
        stack_next = update_hard_stack(
            stack_t, stack_pushed, stack_merged, buffer_top_t,
            merge_value, mask)

        # Move buffer cursor as necessary. Since mask == 1 when merge, we
        # should increment each buffer cursor by 1 - mask
        buffer_cur_next = buffer_cur_t + (1 - mask)

        if self._predict_network is not None:
            ret_val = stack_next, buffer_cur_next, tracking_hidden, actions_t
        else:
            ret_val = stack_next, buffer_cur_next, tracking_hidden
        if not self.interpolate:
            # Use ss_mask as a redundant return value.
            ret_val = (ss_mask_gen_matrix_t,) + ret_val
        return ret_val

    def _make_scan(self):
        """Build the sequential composition / scan graph."""

        batch_size, max_stack_size = self.X.shape

        # Stack batch is a 3D tensor.
        stack_shape = (batch_size, max_stack_size, self.model_dim)
        stack_init = T.zeros(stack_shape)

        # Allocate two helper stack copies (passed as non_seqs into scan).
        stack_pushed = T.zeros(stack_shape)
        stack_merged = T.zeros(stack_shape)

        # Look up all of the embeddings that will be used.
        raw_embeddings = self.embeddings[self.X]  # batch_size * seq_length * emb_dim

        # Allocate a "buffer" stack initialized with projected embeddings,
        # and maintain a cursor in this buffer.
        buffer_t = self._embedding_projection_network(
            raw_embeddings, self.word_embedding_dim, self.model_dim, self._vs, name="project")

        if self.use_input_batch_norm:
            buffer_t = util.BatchNorm(buffer_t, self.model_dim, self._vs, "buffer", self.training_mode,
                axes=[0, 1])
        if self.use_input_dropout:
            buffer_t = util.Dropout(buffer_t, self.embedding_dropout_keep_rate, self.training_mode)


        # Collapse buffer to (batch_size * buffer_size) * emb_dim for fast indexing.
        buffer_t = buffer_t.reshape((-1, self.model_dim))

        buffer_cur_init = T.zeros((batch_size,), dtype="int")

        DUMMY = T.zeros((2,)) # a dummy tensor used as a place-holder

        # Dimshuffle inputs to seq_len * batch_size for scanning
        transitions = self.transitions.dimshuffle(1, 0)

        # Initialize the hidden state for the tracking LSTM, if needed.
        if self.use_tracking_lstm:
            # TODO: Unify what 'dim' means with LSTM. Here, it's the dim of
            # each of h and c. For 'model_dim', it's the combined dimension
            # of the full hidden state (so h and c are each model_dim/2).
            hidden_init = T.zeros((batch_size, self.tracking_lstm_hidden_dim * 2))
        else:
            hidden_init = DUMMY

        # Set up the output list for scanning over _step().
        if self._predict_network is not None:
            outputs_info = [stack_init, buffer_cur_init, hidden_init, None]
        else:
            outputs_info = [stack_init, buffer_cur_init, hidden_init]

        # Prepare data to scan over.
        sequences = [transitions]
        if self.interpolate:
            # Generate Bernoulli RVs to simulate scheduled sampling
            # if the interpolate flag is on.
            ss_mask_gen_matrix = self.ss_mask_gen.binomial(
                                transitions.shape, p=self.ss_prob)
            # Take in the RV sequence as input.
            sequences.append(ss_mask_gen_matrix)
        else:
            # Take in the RV sequqnce as a dummy output. This is
            # done to avaid defining another step function.
            outputs_info = [DUMMY] + outputs_info

        scan_ret = theano.scan(
                self._step,
                sequences=sequences,
                non_sequences=[stack_pushed, stack_merged,
                        buffer_t, self.ground_truth_transitions_visible],
                outputs_info=outputs_info)[0]

        stack_ind = 0 if self.interpolate else 1
        self.final_stack = scan_ret[stack_ind][-1]
        self.embeddings = self.final_stack[:, 0]

        self.transitions_pred = None
        if self._predict_network is not None:
            self.transitions_pred = scan_ret[-1].dimshuffle(1, 0, 2)


class Model0(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = None
        kwargs["train_with_predicted_transitions"] = False
        kwargs["interpolate"] = False
        super(Model0, self).__init__(*args, **kwargs)


class Model1(HardStack):

    def __init__(self, *args, **kwargs):
        # set the tracking unit based on supplied tracking_lstm_hidden_dim
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["predict_network"] = util.TrackingUnit
        else:
            kwargs["predict_network"] = util.Linear
        # defaults to not using predictions while training and not using scheduled sampling
        kwargs["train_with_predicted_transitions"] = False
        kwargs["interpolate"] = False
        super(Model1, self).__init__(*args, **kwargs)


class Model2(HardStack):

    def __init__(self, *args, **kwargs):
        # set the tracking unit based on supplied tracking_lstm_hidden_dim
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["predict_network"] = util.TrackingUnit
        else:
            kwargs["predict_network"] = util.Linear
        # defaults to using predictions while training and not using scheduled sampling
        kwargs["train_with_predicted_transitions"] = True
        kwargs["interpolate"] = False
        super(Model2, self).__init__(*args, **kwargs)


class Model2S(HardStack):

    def __init__(self, *args, **kwargs):
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["predict_network"] = util.TrackingUnit
        else:
            kwargs["predict_network"] = util.Linear
        # use supplied settings and use scheduled sampling
        kwargs["train_with_predicted_transitions"] = True
        kwargs["interpolate"] = True
        super(Model2S, self).__init__(*args, **kwargs)

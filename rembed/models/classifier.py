"""From the project root directory (containing data files), this can be run with:

Boolean logic evaluation:
python -m rembed.models.classifier --training_data_path bl-data/pbl_train.tsv \
       --eval_data_path bl-data/pbl_dev.tsv

SST sentiment (Demo only, model needs a full GloVe embeddings file to do well):
python -m rembed.models.classifier --data_type sst --training_data_path sst-data/train.txt \
       --eval_data_path sst-data/dev.txt --embedding_data_path rembed/tests/test_embedding_matrix.5d.txt \
       --model_dim 10

SNLI entailment (Demo only, model needs a full GloVe embeddings file to do well):
python -m rembed.models.classifier --data_type snli --training_data_path snli_1.0/snli_1.0_dev.jsonl \
       --eval_data_path snli_1.0/snli_1.0_dev.jsonl --embedding_data_path rembed/tests/test_embedding_matrix.5d.txt \
       --model_dim 10
"""

from functools import partial
import pprint
import sys

import gflags
import numpy as np
import tensorflow as tf

from rembed import afs_safe_logger
from rembed import util
from rembed.data.boolean import load_boolean_data
from rembed.data.sst import load_sst_data
from rembed.data.snli import load_snli_data

import rembed.stack


FLAGS = gflags.FLAGS


def build_sentence_model(cls, vocab_size, seq_length, tokens, transitions,
                         num_classes, apply_dropout, vs, initial_embeddings=None, project_embeddings=False):
    """
    Construct a classifier which makes use of some hard-stack model.

    Args:
      cls: Hard stack class to use (from e.g. `rembed.stack`)
      vocab_size:
      seq_length: Length of each sequence provided to the stack model
      tokens: Theano batch (integer matrix), `batch_size * seq_length`
      transitions: Theano batch (integer matrix), `batch_size * seq_length`
      num_classes: Number of output classes
      apply_dropout: 1.0 at training time, 0.0 at eval time (to avoid corrupting outputs in dropout)
      vs: Variable store.
    """

    # Prepare layer which performs stack element composition.
    if FLAGS.lstm_composition:
        compose_network = partial(util.TreeLSTMLayer,
                                  initializer=util.UniformInitializer(FLAGS.init_range))
    else:
        compose_network = partial(util.ReLULayer,
                                  initializer=util.DoubleIdentityInitializer(FLAGS.double_identity_init_range))

    if project_embeddings:
        embedding_projection_network = util.Linear
    else:
        assert FLAGS.word_embedding_dim == FLAGS.model_dim, \
            "word_embedding_dim must equal model_dim unless a projection layer is used."
        embedding_projection_network = util.IdentityLayer

    # Build hard stack which scans over input sequence.
    stack = cls(
        FLAGS.model_dim, FLAGS.word_embedding_dim, vocab_size, FLAGS.batch_size, seq_length,
        compose_network, embedding_projection_network, apply_dropout, vs,
        X=tokens,
        transitions=transitions,
        initial_embeddings=initial_embeddings,
        embedding_dropout_keep_rate=FLAGS.embedding_keep_rate)

    # Extract top element of final stack timestep.
    final_stack = stack.final_stack
    stack_top = tf.slice(final_stack, [FLAGS.batch_size * (FLAGS.seq_length - 1), 0], [-1, -1])
    sentence_vector = stack_top  # DEV works?

    sentence_vector = util.Dropout(sentence_vector, FLAGS.semantic_classifier_keep_rate, apply_dropout)

    # Feed forward through a single output layer
    logits = util.Linear(
        sentence_vector, FLAGS.model_dim, num_classes, vs, use_bias=True)

    return stack, stack.transitions_pred, logits



def build_sentence_pair_model(cls, vocab_size, seq_length, tokens, transitions,
                     num_classes, apply_dropout, vs, initial_embeddings=None, project_embeddings=False):
    """
    Construct a classifier which makes use of some hard-stack model.

    Args:
      cls: Hard stack class to use (from e.g. `rembed.stack`)
      vocab_size:
      seq_length: Length of each sequence provided to the stack model
      tokens: Theano batch (integer matrix), `batch_size * seq_length`
      transitions: Theano batch (integer matrix), `batch_size * seq_length`
      num_classes: Number of output classes
      apply_dropout: 1.0 at training time, 0.0 at eval time (to avoid corrupting outputs in dropout)
      vs: Variable store.
    """

    # Prepare layer which performs stack element composition.
    compose_network = partial(util.ReLULayer,
                              initializer=util.DoubleIdentityInitializer(FLAGS.double_identity_init_range))

    if project_embeddings:
        embedding_projection_network = util.Linear
    else:
        assert FLAGS.word_embedding_dim == FLAGS.model_dim, \
            "word_embedding_dim must equal model_dim unless a projection layer is used."
        embedding_projection_network = util.IdentityLayer

    # Split the two sentences
    premise_tokens = tokens[:, :, 0]
    hypothesis_tokens = tokens[:, :, 1]

    premise_transitions = transitions[:, :, 0]
    hypothesis_transitions = transitions[:, :, 1]

    # Build two hard stack models which scan over input sequences.
    premise_model = cls(
        FLAGS.model_dim, FLAGS.word_embedding_dim, vocab_size, seq_length,
        compose_network, embedding_projection_network, apply_dropout, vs,
        X=premise_tokens,
        transitions=premise_transitions,
        initial_embeddings=initial_embeddings,
        embedding_dropout_keep_rate=FLAGS.embedding_keep_rate)
    hypothesis_model = cls(
        FLAGS.model_dim, FLAGS.word_embedding_dim, vocab_size, seq_length,
        compose_network, embedding_projection_network, apply_dropout, vs,
        X=hypothesis_tokens,
        transitions=hypothesis_transitions,
        initial_embeddings=initial_embeddings,
        embedding_dropout_keep_rate=FLAGS.embedding_keep_rate)

    # Extract top element of final stack timestep.
    premise_stack_top = premise_model.final_stack[:, 0]
    hypothesis_stack_top = hypothesis_model.final_stack[:, 0]

    premise_vector = premise_stack_top.reshape((-1, FLAGS.model_dim))
    hypothesis_vector = hypothesis_stack_top.reshape((-1, FLAGS.model_dim))

    # Concatenate and apply dropout
    mlp_input = T.concatenate([premise_vector, hypothesis_vector], axis=1)
    dropout_mlp_input = util.Dropout(mlp_input, FLAGS.semantic_classifier_keep_rate, apply_dropout)

    # Apply a combining MLP
    pair_features = util.MLP(dropout_mlp_input, 2 * FLAGS.model_dim, FLAGS.model_dim, vs, hidden_dims=[FLAGS.model_dim],
        name="combining_mlp")

    # Feed forward through a single output layer
    logits = util.Linear(
        pair_features, FLAGS.model_dim, num_classes, vs, use_bias=True)

    return premise_model.transitions_pred, hypothesis_model.transitions_pred, logits


def build_cost(logits, targets, batch_size, num_classes):
    """
    Build a classification cost function.
    """

    # densify the sparse targets (turn into one-hot matrix)
    targets_dense = util.convert_labels_to_onehot(targets, batch_size, num_classes)

    # # Clip gradients coming from the cost function.
    # logits = theano.gradient.grad_clip(
    #     logits, -1. * FLAGS.clipping_max_norm, FLAGS.clipping_max_norm)

    costs = tf.nn.softmax_cross_entropy_with_logits(logits, targets_dense)
    cost = tf.reduce_mean(costs)

    pred = tf.to_int32(tf.argmax(logits, 1))
    acc = 1. - tf.reduce_mean(tf.to_float(tf.not_equal(pred, targets)))

    return cost, acc


def build_action_cost(logits, targets, num_transitions):
    """
    Build a parse action prediction cost function.
    """

    # swap seq_length dimension to front so that we can scan per timestep
    logits = T.swapaxes(logits, 0, 1)
    targets = targets.T

    def cost_t(logits, tgt):
        # TODO(jongauthier): Taper down xent cost as we proceed through
        # sequence?
        predicted_dist = T.nnet.softmax(logits)
        cost = T.nnet.categorical_crossentropy(predicted_dist, tgt)

        pred = T.argmax(logits, axis=1)
        error = T.neq(pred, tgt)
        return cost, error

    results, _ = theano.scan(cost_t, [logits, targets])
    costs, errors = results

    # Create a mask that selects only transitions that involve real data.
    unrolling_length = T.shape(costs)[0]
    padding = unrolling_length - num_transitions
    padding = T.reshape(padding, (1, -1))
    rng = T.arange(unrolling_length) + 1
    rng = T.reshape(rng, (-1, 1))
    mask = T.gt(rng, padding)

    # Compute acc using the mask
    acc = 1 - T.cast(T.sum(errors * mask), theano.config.floatX) / T.sum(num_transitions)

    # Compute cost directly, since we *do* want a cost incentive to get the padding
    # transitions right.
    cost = T.mean(costs)
    return cost, acc


def train():
    logger = afs_safe_logger.Logger(FLAGS.experiment_name + ".log")

    if FLAGS.data_type == "bl":
        data_manager = load_boolean_data
    elif FLAGS.data_type == "sst":
        data_manager = load_sst_data
    elif FLAGS.data_type == "snli":
        data_manager = load_snli_data
    else:
        logger.Log("Bad data type.")
        return

    pp = pprint.PrettyPrinter(indent=4)
    logger.Log("Flag values:\n" + pp.pformat(FLAGS.FlagValuesDict()))

    # Load the data.
    raw_training_data, vocabulary = data_manager.load_data(
        FLAGS.training_data_path)

    # Load the eval data.
    raw_eval_sets = []
    if FLAGS.eval_data_path:
        for eval_filename in FLAGS.eval_data_path.split(":"):
            eval_data, _ = data_manager.load_data(eval_filename)
            raw_eval_sets.append((eval_filename, eval_data))

    # Prepare the vocabulary.
    if not vocabulary:
        logger.Log("In open vocabulary mode. Using loaded embeddings without fine-tuning.")
        train_embeddings = False
        vocabulary = util.BuildVocabulary(
            raw_training_data, raw_eval_sets, FLAGS.embedding_data_path, logger=logger,
            sentence_pair_data=data_manager.SENTENCE_PAIR_DATA)
    else:
        logger.Log("In fixed vocabulary mode. Training embeddings.")
        train_embeddings = True

    # Load pretrained embeddings.
    if FLAGS.embedding_data_path:
        logger.Log("Loading vocabulary with " + str(len(vocabulary))
                   + " words from " + FLAGS.embedding_data_path)
        initial_embeddings = util.LoadEmbeddingsFromASCII(
            vocabulary, FLAGS.word_embedding_dim, FLAGS.embedding_data_path)
    else:
        initial_embeddings = None

    # Trim dataset, convert token sequences to integer sequences, crop, and
    # pad.
    logger.Log("Preprocessing training data.")
    training_data = util.PreprocessDataset(
        raw_training_data, vocabulary, FLAGS.seq_length, data_manager, eval_mode=False, logger=logger,
        sentence_pair_data=data_manager.SENTENCE_PAIR_DATA)
    training_data_iter = util.MakeTrainingIterator(
        training_data, FLAGS.batch_size)

    eval_iterators = []
    for filename, raw_eval_set in raw_eval_sets:
        logger.Log("Preprocessing eval data: " + filename)
        e_X, e_transitions, e_y, e_num_transitions = util.PreprocessDataset(
            raw_eval_set, vocabulary, FLAGS.seq_length, data_manager, eval_mode=True, logger=logger,
            sentence_pair_data=data_manager.SENTENCE_PAIR_DATA)
        eval_iterators.append((filename,
            util.MakeEvalIterator((e_X, e_transitions, e_y, e_num_transitions), FLAGS.batch_size)))

    # Set up the placeholders.
    y = tf.placeholder(tf.int32, shape=(None,), name="y")
    apply_dropout = tf.placeholder(tf.float32, name="apply_dropout") # 1: Training with dropout, 0: Eval

    logger.Log("Building model.")
    vs = util.VariableStore(
        default_initializer=util.UniformInitializer(FLAGS.init_range), logger=logger)
    model_cls = getattr(rembed.stack, FLAGS.model_type)
    if data_manager.SENTENCE_PAIR_DATA:
        X = T.itensor3("X")
        transitions = T.itensor3("transitions")
        num_transitions = T.imatrix("num_transitions")

        predicted_premise_transitions, predicted_hypothesis_transitions, logits = build_sentence_pair_model(
            model_cls, len(vocabulary), FLAGS.seq_length,
            X, transitions, len(data_manager.LABEL_MAP), apply_dropout, vs,
            initial_embeddings=initial_embeddings, project_embeddings=(not train_embeddings))
    else:
        X = tf.placeholder(tf.int32, name="X")
        transitions = tf.placeholder(tf.int32, shape=(FLAGS.batch_size, FLAGS.seq_length), name="transitions")
        num_transitions = tf.placeholder(tf.int32, name="num_transitions")

        stack, predicted_transitions, logits = build_sentence_model(
            model_cls, len(vocabulary), FLAGS.seq_length,
            X, transitions, len(data_manager.LABEL_MAP), apply_dropout, vs,
            initial_embeddings=initial_embeddings, project_embeddings=(not train_embeddings))

    xent_cost, acc = build_cost(logits, y, FLAGS.batch_size, len(data_manager.LABEL_MAP))

    # Set up L2 regularization.
    l2_cost = 0.0
    for var in vs.vars:
        if "embedding" not in var:
            l2_cost += FLAGS.l2_lambda * tf.reduce_sum(tf.square(vs.vars[var]))

    # Compute cross-entropy cost on action predictions.
    if (not data_manager.SENTENCE_PAIR_DATA) and predicted_transitions is not None:
        action_cost, action_acc = build_action_cost(predicted_transitions, transitions, num_transitions)
    if data_manager.SENTENCE_PAIR_DATA and predicted_hypothesis_transitions is not None:
        p_action_cost, p_action_acc = build_action_cost(predicted_premise_transitions, transitions[:, :, 0], num_transitions[:, 0])
        h_action_cost, h_action_acc = build_action_cost(predicted_premise_transitions, transitions[:, :, 1], num_transitions[:, 1])
        action_cost = p_action_cost + h_action_cost
        action_acc = (p_action_acc + h_action_acc) / 2  # TODO(SB): Average over transitions, not words.
    else:
        action_cost = tf.constant(0.0)
        action_acc = tf.constant(0.0)

    # TODO(jongauthier): Add hyperparameter for trading off action cost vs xent
    # cost
    total_cost = xent_cost + l2_cost + action_cost

    # Set up optimization.
    if train_embeddings:
        trained_params = vs.vars.values()
    else:
        trained_params = [vs.vars[key] for key in vs.vars if 'embedding' not in key]

#    optim = tf.train.RMSPropOptimizer(FLAGS.learning_rate, 0.9, epsilon=1e-6)
    optim = tf.train.GradientDescentOptimizer(FLAGS.learning_rate)
    train_op = optim.minimize(total_cost)
    # Training open-vocabulary embeddings is a questionable idea right now. Disabled:
    # new_values.append(
    #     util.embedding_SGD(total_cost, embedding_params, embedding_lr))

    # # Create training and eval functions.
    # # Unused variable warnings are supressed so that num_transitions can be passed in when training Model 0,
    # # which ignores it. This yields more readable code that is very slightly slower.
    # logger.Log("Building update function.")
    # update_fn = theano.function(
    #     [X, transitions, y, num_transitions, lr, apply_dropout],
    #     [total_cost, xent_cost, action_cost, action_acc, l2_cost, acc],
    #     updates=new_values,
    #     on_unused_input='warn')
    # logger.Log("Building eval function.")
    # eval_fn = theano.function([X, transitions, y, num_transitions, apply_dropout], [acc, action_acc],
    #     on_unused_input='warn')
    # logger.Log("Training.")

    sess = tf.Session()
    sess.run(tf.initialize_all_variables())

    train_fetch = (train_op, total_cost, xent_cost, action_cost, action_acc, l2_cost, acc)
    eval_fetch = (acc, action_acc)

    # Main training loop.
    for step in range(FLAGS.training_steps):
        stack.zero(sess)

        X_batch, transitions_batch, y_batch, num_transitions_batch = training_data_iter.next()
        if len(X_batch) != FLAGS.batch_size:
            continue
        ret = sess.run(train_fetch, {X: X_batch,
                                     transitions: transitions_batch,
                                     y: y_batch,
                                     num_transitions: num_transitions_batch,
                                     apply_dropout: 1.0})
        _, total_cost_val, xent_cost_val, action_cost_val, action_acc_val, l2_cost_val, acc_val = ret

        if step % FLAGS.statistics_interval_steps == 0:
            logger.Log(
                "Step: %i\tAcc: %f\t%f\tCost: %5f %5f %5f %5f"
                % (step, acc_val, action_acc_val, total_cost_val, xent_cost_val, action_cost_val,
                   l2_cost_val))

        if step % FLAGS.eval_interval_steps == 0:
            for eval_set in eval_iterators:
                # Evaluate
                acc_accum = 0.0
                action_acc_accum = 0.0
                eval_batches = 0.0
                for (eval_X_batch, eval_transitions_batch, eval_y_batch, eval_num_transitions_batch) in eval_set[1]:
                    acc_value, action_acc_value = sess.run(
                            eval_fetch,
                            {X: eval_X_batch,
                             transitions: eval_transitions_batch,
                             y: eval_y_batch,
                             num_transitions: eval_num_transitions_batch,
                             apply_dropout: 0.0})
                    acc_accum += acc_value
                    action_acc_accum += action_acc_value
                    eval_batches += 1.0
                logger.Log("Step: %i\tEval acc: %f\t %f\t%s" %
                          (step, acc_accum / eval_batches, action_acc_accum / eval_batches, eval_set[0]))


    sess.close()

if __name__ == '__main__':
    # Experiment naming.
    gflags.DEFINE_string("experiment_name", "experiment", "")

    # Data types.
    gflags.DEFINE_string("data_type", "bl", "Values: bl, sst, snli")

    # Data settings.
    gflags.DEFINE_string("training_data_path", None, "")
    gflags.DEFINE_string("eval_data_path", None, "")
    gflags.DEFINE_integer("seq_length", 30, "")
    gflags.DEFINE_integer("eval_seq_length", 30, "")

    gflags.DEFINE_string("embedding_data_path", None,
                         "If set, load GloVe formatted embeddings from here.")

    # Model architecture settings.
    gflags.DEFINE_enum("model_type", "Model0",
                       ["Model0", "Model1", "Model2"],
                       "")
    gflags.DEFINE_integer("model_dim", 5, "")
    gflags.DEFINE_integer("word_embedding_dim", 5, "")
    gflags.DEFINE_float("semantic_classifier_keep_rate", 0.5,
        "Used for dropout in the semantic task classifier.")
    gflags.DEFINE_float("embedding_keep_rate", 0.5,
        "Used for dropout on transformed embeddings.")
    gflags.DEFINE_boolean("lstm_composition", False, "")
    # gflags.DEFINE_integer("num_composition_layers", 1, "")

    # Optimization settings.
    gflags.DEFINE_integer("training_steps", 1000000, "")
    gflags.DEFINE_integer("batch_size", 32, "")
    gflags.DEFINE_float("learning_rate", 0.001, "Used in RMSProp.")
    # gflags.DEFINE_float("momentum", 0.9, "")
    gflags.DEFINE_float("clipping_max_norm", 1.0, "")
    gflags.DEFINE_float("l2_lambda", 1e-5, "")
    gflags.DEFINE_float("init_range", 0.01, "")
    gflags.DEFINE_float("double_identity_init_range", 0.001, "")

    # Display settings.
    gflags.DEFINE_integer("statistics_interval_steps", 50, "")
    gflags.DEFINE_integer("eval_interval_steps", 50, "")

    # Parse command line flags.
    FLAGS(sys.argv)

    # Run.
    train()

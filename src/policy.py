"""
NN Policy with KL Divergence Constraint (PPO / TRPO)

Written by Patrick Coady (pat-coady.github.io)
"""
import numpy as np
import tensorflow as tf
import tflearn as tfl
import os

class Policy(object):
    """ NN-based policy approximation """
    def __init__(self, obs_dim, act_dim, kl_targ, hid1_mult, policy_logvar, clipping_range=None):
        """
        Args:
            obs_dim: num observation dimensions (int)
            act_dim: num action dimensions (int)
            kl_targ: target KL divergence between pi_old and pi_new
            hid1_mult: size of first hidden layer, multiplier of obs_dim
            policy_logvar: natural log of initial policy variance
        """
        self.beta = 1.0  # dynamically adjusted D_KL loss multiplier
        self.eta = 50  # multiplier for D_KL-kl_targ hinge-squared loss
        self.kl_targ = kl_targ
        self.hid1_mult = hid1_mult
        self.policy_logvar = policy_logvar
        self.epochs = 20
        self.lr = None
        self.lr_multiplier = 1.0  # dynamically adjust lr when D_KL out of control
        self.obs_dim = obs_dim
        self.act_dim = act_dim
        self.clipping_range = clipping_range
        self._build_graph()
        self._init_session()

        self.save_path = "agents/ppo/policy"
        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)

    def _build_graph(self):
        """ Build and initialize TensorFlow graph """
        self.g = tf.Graph()
        with self.g.as_default():
            self._placeholders()
            self._policy_nn()
            self._logprob()
            self._kl_entropy()
            self._sample()
            self._loss_train_op()
            self.init = tf.global_variables_initializer()

    def _placeholders(self):
        """ Input placeholders"""
        # observations, actions and advantages:
        self.prev_torque_ph = tf.placeholder(tf.float32, (None, 8), 'p_trq')
        self.current_angle_ph = tf.placeholder(tf.float32, (None, 8), 'c_ang')
        self.act_ph = tf.placeholder(tf.float32, (None, self.act_dim), 'act')
        self.advantages_ph = tf.placeholder(tf.float32, (None,), 'advantages')
        # strength of D_KL loss terms:
        self.beta_ph = tf.placeholder(tf.float32, (), 'beta')
        self.eta_ph = tf.placeholder(tf.float32, (), 'eta')
        # learning rate:
        self.lr_ph = tf.placeholder(tf.float32, (), 'eta')
        # log_vars and means with pi_old (previous step's policy parameters):
        self.old_log_vars_ph = tf.placeholder(tf.float32, (self.act_dim,), 'old_log_vars')
        self.old_means_ph = tf.placeholder(tf.float32, (None, self.act_dim), 'old_means')

    def _coxa_net(self, t_ind, a_ind, reuse):
        w_init = tfl.initializations.xavier(uniform=True)
        input = tf.concat([*[self.prev_torque_ph[:, i:i + 1] for i in t_ind], self.current_angle_ph[:, a_ind : a_ind + 1]], 1)
        with tf.variable_scope("coxa", reuse=reuse):
            tmp_l = tfl.fully_connected(input, 6, activation='relu', weights_init=w_init)
            c_out = tfl.fully_connected(tmp_l, 1, activation='tanh', weights_init=w_init)
            return c_out

    def _femur_net(self, t_ind, a_ind, reuse):
        w_init = tfl.initializations.xavier(uniform=True)
        input = tf.concat([*[self.prev_torque_ph[:, i:i + 1] for i in t_ind], self.current_angle_ph[:, a_ind:a_ind+1]], 1)
        with tf.variable_scope("femur", reuse=reuse):
            tmp_l = tfl.fully_connected(input, 5, activation='relu', weights_init=w_init)
            f_out = tfl.fully_connected(tmp_l, 1, activation='tanh', weights_init=w_init)
            return f_out

    def _policy_nn(self):
        """ Neural net for policy approximation function

        Policy parameterized by Gaussian means and variances. NN outputs mean
         action based on observation. Trainable variables hold log-variances
         for each action dimension (i.e. variances not determined by NN).
        """

        c0_out = self._coxa_net([0, 1, 2, 4, 6], 0, reuse=False)
        c1_out = self._coxa_net([0, 2, 3, 4, 6], 2, reuse=True)
        c2_out = self._coxa_net([0, 2, 4, 5, 6], 4, reuse=True)
        c3_out = self._coxa_net([0, 2, 4, 6, 7], 6, reuse=True)

        f0_out = self._femur_net([0, 1], 0, reuse=False)
        f1_out = self._femur_net([2, 3], 3, reuse=True)
        f2_out = self._femur_net([4, 5], 5, reuse=True)
        f3_out = self._femur_net([5, 6], 7, reuse=True)

        self.means = tf.concat([c0_out, f0_out, c1_out, f1_out, c2_out, f2_out, c3_out, f3_out], 1)

        self.lr = 1e-4

        logvar_speed = 10
        log_vars = tf.get_variable('logvars', (logvar_speed, self.act_dim), tf.float32,
                                   tf.constant_initializer(0.0))
        self.log_vars = tf.reduce_sum(log_vars, axis=0) + self.policy_logvar


    def _logprob(self):
        """ Calculate log probabilities of a batch of observations & actions

        Calculates log probabilities using previous step's model parameters and
        new parameters being trained.
        """
        logp = -0.5 * tf.reduce_sum(self.log_vars)
        logp += -0.5 * tf.reduce_sum(tf.square(self.act_ph - self.means) / tf.exp(self.log_vars), axis=1)
        self.logp = logp

        logp_old = -0.5 * tf.reduce_sum(self.old_log_vars_ph)
        logp_old += -0.5 * tf.reduce_sum(tf.square(self.act_ph - self.old_means_ph) /
                                         tf.exp(self.old_log_vars_ph), axis=1)
        self.logp_old = logp_old

    def _kl_entropy(self):
        """
        Add to Graph:
            1. KL divergence between old and new distributions
            2. Entropy of present policy given states and actions

        https://en.wikipedia.org/wiki/Multivariate_normal_distribution#Kullback.E2.80.93Leibler_divergence
        https://en.wikipedia.org/wiki/Multivariate_normal_distribution#Entropy
        """
        log_det_cov_old = tf.reduce_sum(self.old_log_vars_ph)
        log_det_cov_new = tf.reduce_sum(self.log_vars)
        tr_old_new = tf.reduce_sum(tf.exp(self.old_log_vars_ph - self.log_vars))

        self.kl = 0.5 * tf.reduce_mean(log_det_cov_new - log_det_cov_old + tr_old_new +
                                       tf.reduce_sum(tf.square(self.means - self.old_means_ph) /
                                                     tf.exp(self.log_vars), axis=1) -
                                       self.act_dim)
        self.entropy = 0.5 * (self.act_dim * (np.log(2 * np.pi) + 1) +
                              tf.reduce_sum(self.log_vars))

    def _sample(self):
        """ Sample from distribution, given observation """
        self.sampled_act = (self.means +
                            tf.exp(self.log_vars / 2.0) *
                            tf.random_normal(shape=(self.act_dim,)))

    def _loss_train_op(self):
        """
        Three loss terms:
            1) standard policy gradient
            2) D_KL(pi_old || pi_new)
            3) Hinge loss on [D_KL - kl_targ]^2

        See: https://arxiv.org/pdf/1707.02286.pdf
        """
        if self.clipping_range is not None:
            print('setting up loss with clipping objective')
            pg_ratio = tf.exp(self.logp - self.logp_old)
            clipped_pg_ratio = tf.clip_by_value(pg_ratio, 1 - self.clipping_range[0], 1 + self.clipping_range[1])
            surrogate_loss = tf.minimum(self.advantages_ph * pg_ratio,
                                        self.advantages_ph * clipped_pg_ratio)
            self.loss = -tf.reduce_mean(surrogate_loss)
        else:
            print('setting up loss with KL penalty')
            loss1 = -tf.reduce_mean(self.advantages_ph *
                                    tf.exp(self.logp - self.logp_old))
            loss2 = tf.reduce_mean(self.beta_ph * self.kl)
            loss3 = self.eta_ph * tf.square(tf.maximum(0.0, self.kl - 2.0 * self.kl_targ))
            self.loss = loss1 + loss2 + loss3

        self.control_effort = tf.reduce_sum(tf.square(self.means))

        optimizer = tf.train.AdamOptimizer(self.lr_ph)
        ctrl_optimizer = tf.train.AdamOptimizer(1e-4)
        self.train_op = optimizer.minimize(self.loss)
        self.relax_op = ctrl_optimizer.minimize(self.control_effort)

    def _init_session(self):
        """Launch TensorFlow session and initialize variables"""
        self.sess = tf.Session(graph=self.g)
        self.sess.run(self.init)

    def sample(self, obs):
        """Draw sample from policy distribution"""
        feed_dict = {self.prev_torque_ph: obs[0:1, :8],
                     self.current_angle_ph: obs[0:1, 8:16]}

        return self.sess.run(self.sampled_act, feed_dict=feed_dict)

    def update(self, observes, actions, advantages, logger):
        """ Update policy based on observations, actions and advantages

        Args:
            observes: observations, shape = (N, obs_dim)
            actions: actions, shape = (N, act_dim)
            advantages: advantages, shape = (N,)
            logger: Logger object, see utils.py
        """
        feed_dict = {self.prev_torque_ph: observes[:, :8],
                     self.current_angle_ph: observes[:, 8:16],
                     self.act_ph: actions,
                     self.advantages_ph: advantages,
                     self.beta_ph: self.beta,
                     self.eta_ph: self.eta,
                     self.lr_ph: self.lr * self.lr_multiplier}
        old_means_np, old_log_vars_np = self.sess.run([self.means, self.log_vars],
                                                      feed_dict)
        feed_dict[self.old_log_vars_ph] = old_log_vars_np
        feed_dict[self.old_means_ph] = old_means_np
        loss, kl, entropy = 0, 0, 0
        for e in range(self.epochs):
            # TODO: need to improve data pipeline - re-feeding data every epoch
            self.sess.run(self.train_op, feed_dict)
            loss, kl, entropy = self.sess.run([self.loss, self.kl, self.entropy], feed_dict)
            if kl > self.kl_targ * 4:  # early stopping if D_KL diverges badly
                break
        # TODO: too many "magic numbers" in next 8 lines of code, need to clean up
        if kl > self.kl_targ * 2:  # servo beta to reach D_KL target
            self.beta = np.minimum(35, 1.5 * self.beta)  # max clip beta
            if self.beta > 30 and self.lr_multiplier > 0.1:
                self.lr_multiplier /= 1.5
        elif kl < self.kl_targ / 2:
            self.beta = np.maximum(1 / 35, self.beta / 1.5)  # min clip beta
            if self.beta < (1 / 30) and self.lr_multiplier < 10:
                self.lr_multiplier *= 1.5

        # Relax outputs
        self.sess.run(self.relax_op, feed_dict)

        logger.log({'PolicyLoss': loss,
                    'PolicyEntropy': entropy,
                    'KL': kl,
                    'Beta': self.beta,
                    '_lr_multiplier': self.lr_multiplier})



    def close_sess(self):
        """ Close TensorFlow session """
        self.sess.close()

    def save_weights(self):
        with self.g.as_default():
            saver = tf.train.Saver()
            saver.save(self.sess, os.path.join(self.save_path, "trained_model"))

    def restore_weights(self):
        with self.g.as_default():
            saver = tf.train.Saver()
            saver.restore(self.sess, tf.train.latest_checkpoint(self.save_path))

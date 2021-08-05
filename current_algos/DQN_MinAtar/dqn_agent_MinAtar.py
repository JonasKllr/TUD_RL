import copy
import math
import pickle

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from current_algos.DQN_MinAtar.dqn_buffer_MinAtar import UniformReplayBuffer_CNN
from current_algos.DQN_MinAtar.dqn_nets_MinAtar import CNN_DQN
from current_algos.common.normalizer import Input_Normalizer
from current_algos.common.logging_func import *

class CNN_DQN_Agent:
    def __init__(self, 
                 mode,
                 num_actions, 
                 state_shape,
                 dqn_weights      = None, 
                 input_norm       = False,
                 input_norm_prior = None,
                 double           = False,
                 gamma            = 0.99,
                 eps_decay        = 0.995,
                 eps_final        = 0.001,
                 n_steps          = 1,
                 tgt_update_freq  = 256,
                 lr               = 0.001,
                 l2_reg           = 0.0,
                 buffer_length    = 10000,
                 grad_clip        = False,
                 grad_rescale     = False,
                 act_start_step   = 10000,
                 upd_start_step   = 1000,
                 upd_every        = 1,
                 batch_size       = 128,
                 device           = "cpu"):
        """Initializes agent. Agent can select actions based on his model, memorize and replay to train his model.

        Args:
            mode ([type]): [description]
            num_actions ([type]): [description]
            state_dim ([type]): [description]
            action_high ([type]): [description]
            action_low ([type]): [description]
            actor_weights ([type], optional): [description]. Defaults to None.
            critic_weights ([type], optional): [description]. Defaults to None.
            input_norm (bool, optional): [description]. Defaults to False.
            input_norm_prior ([type], optional): [description]. Defaults to None.
            gamma (float, optional): [description]. Defaults to 0.99.
            tau (float, optional): [description]. Defaults to 0.005.
            lr_actor (float, optional): [description]. Defaults to 0.001.
            lr_critic (float, optional): [description]. Defaults to 0.001.
            buffer_length (int, optional): [description]. Defaults to 1000000.
            grad_clip (bool, optional): [description]. Defaults to False.
            grad_rescale (bool, optional): [description]. Defaults to False.
            act_start_step (int, optional): Number of steps with random actions before using own decisions. Defaults to 10000.
            upd_start_step (int, optional): Steps to perform in environment before starting updates. Defaults to 1000.
            upd_every (int, optional): Frequency of performing updates. However, ratio between environment and gradient steps is always 1.
            batch_size (int, optional): [description]. Defaults to 100.
            device (str, optional): [description]. Defaults to "cpu".
        """

        # store attributes and hyperparameters
        assert mode in ["train", "test"], "Unknown mode. Should be 'train' or 'test'."
        assert not (mode == "test" and (dqn_weights is None)), "Need prior weights in test mode."
        self.mode = mode
        
        self.name        = "CNN_DQN_Agent"
        self.num_actions = num_actions
 
        # CNN shape
        assert len(state_shape) == 3 and type(state_shape) == tuple, "'state_shape' should be: (height, width, in_channels)"
        self.state_shape = state_shape

        self.dqn_weights      = dqn_weights
        self.input_norm       = input_norm
        self.input_norm_prior = input_norm_prior
        self.double           = double
        self.gamma            = gamma
        self.epsilon          = 1.0
        self.eps_decay        = eps_decay
        self.eps_final        = eps_final
        self.n_steps          = n_steps
        self.tgt_update_freq  = tgt_update_freq
        self.lr               = lr
        self.l2_reg           = l2_reg
        self.buffer_length    = buffer_length
        self.grad_clip        = grad_clip
        self.grad_rescale     = grad_rescale
        self.act_start_step   = act_start_step
        self.upd_start_step   = upd_start_step
        self.upd_every        = upd_every
        self.batch_size       = batch_size

        # n_step
        assert n_steps >= 1, "'n_steps' should not be smaller than 1."

        # gpu support
        assert device in ["cpu", "cuda"], "Unknown device."

        if device == "cpu":    
            self.device = torch.device("cpu")
        else:
            self.device = torch.device("cuda")
            print("Using GPU support.")
        
        # init logger and save config
        self.logger = EpochLogger()
        self.logger.save_config(locals())
        
        # init replay buffer and noise
        if mode == "train":
            self.replay_buffer = UniformReplayBuffer_CNN(state_shape=state_shape, n_steps=n_steps, gamma=gamma,
                                                         buffer_length=buffer_length, batch_size=batch_size, device=self.device)

        # init input normalizer
        if input_norm:
            assert not (mode == "test" and input_norm_prior is None), "Please supply 'input_norm_prior' in test mode with input normalization."
            
            if input_norm_prior is not None:
                with open(input_norm_prior, "rb") as f:
                    prior = pickle.load(f)
                self.inp_normalizer = Input_Normalizer(state_dim=state_shape, prior=prior)
            else:
                self.inp_normalizer = Input_Normalizer(state_dim=state_shape, prior=None)
        
        # init convolutional DQN
        self.DQN = CNN_DQN(in_channels=state_shape[0], height=state_shape[1], width=state_shape[2], num_actions=num_actions).to(self.device)
        
        print("--------------------------------------------")
        print(f"n_params DQN: {self._count_params(self.DQN)}")
        print("--------------------------------------------")
        
        # load prior weights if available
        if dqn_weights is not None:
            self.DQN.load_state_dict(torch.load(dqn_weights))

        # init target net and counter for target update
        self.target_DQN = copy.deepcopy(self.DQN).to(self.device)
        self.tgt_up_cnt = 0
        
        # freeze target nets with respect to optimizers to avoid unnecessary computations
        for p in self.target_DQN.parameters():
            p.requires_grad = False

        # define optimizer
        self.DQN_optimizer = optim.Adam(self.DQN.parameters(), lr=lr, weight_decay=l2_reg)

    def _count_params(self, net):
        return sum([np.prod(p.shape) for p in net.parameters()])

    @torch.no_grad()
    def select_action(self, s):
        """Epsilon-greedy based action selection for a given state.
        Arg s:   np.array with shape (in_channels, height, width)
        returns: int for the action
        """
        # random action
        if (np.random.binomial(1, self.epsilon) == 1) and (self.mode == "train"):
            a = np.random.randint(low=0, high=self.num_actions, size=1, dtype=int).item()
            
        # greedy action
        else:
            # reshape obs (namely, to torch.Size([1, in_channels, height, width]))
            s = torch.tensor(s.astype(np.float32)).unsqueeze(0).to(self.device)

            # forward pass
            q = self.DQN(s).to(self.device)

            # greedy
            a = torch.argmax(q).item()

        # decay epsilon
        self.epsilon = max(self.epsilon * self.eps_decay, self.eps_final)

        return a

    def memorize(self, s, a, r, s2, d):
        """Stores current transition in replay buffer."""
        self.replay_buffer.add(s, a, r, s2, d)

    def train(self):
        """Samples from replay_buffer, updates critic and the target networks."""        
        # sample batch
        batch = self.replay_buffer.sample()
        
        # unpack batch
        s, a, r, s2, d = batch

        #-------- train DQN --------
        # clear gradients
        self.DQN_optimizer.zero_grad()
        
        # calculate current estimated Q-values
        Q_v = self.DQN(s)
        Q_v = torch.gather(input=Q_v, dim=1, index=a)
 
        # calculate targets
        with torch.no_grad():

            # Q-value of next state-action pair
            if self.double:
                a2 = torch.argmax(self.DQN(s2), dim=1).reshape(self.batch_size, 1)
                target_Q_next = torch.gather(input=self.target_DQN(s2), dim=1, index=a2)
            else:
                target_Q_next = self.target_DQN(s2)
                target_Q_next = torch.max(target_Q_next, dim=1).values.reshape(self.batch_size, 1)

            # target
            target_Q = r + (self.gamma ** self.n_steps) * target_Q_next * (1 - d)

        # calculate loss
        loss = F.mse_loss(Q_v, target_Q)
        
        # compute gradients
        loss.backward()

        # gradient scaling and clipping
        if self.grad_rescale:
            for p in self.DQN.parameters():
                p.grad *= 1 / math.sqrt(2)
        if self.grad_clip:
            nn.utils.clip_grad_norm_(self.DQN.parameters(), max_norm=10)
        
        # perform optimizing step
        self.DQN_optimizer.step()
        
        # log critic training
        self.logger.store(Loss=loss.detach().cpu().numpy().item())
        self.logger.store(Q_val=Q_v.detach().mean().cpu().numpy().item())

        #------- Update target networks -------
        if self.tgt_up_cnt % self.tgt_update_freq == 0:
            self.target_update()

        # increase target-update cnt
        self.tgt_up_cnt += 1
    
    @torch.no_grad()
    def target_update(self):
        """Hard update of target network weights."""
        self.target_DQN.load_state_dict(self.DQN.state_dict())

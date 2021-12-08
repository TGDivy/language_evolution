import numpy as np
import torch as T
from torch import nn
from torch import optim
import os
from torch.nn import Softmax
from torch.distributions.categorical import Categorical
from torch.nn import HuberLoss
from dotmap import DotMap
from framework.utils.base import base_policy, Args
from pettingzoo import ParallelEnv
from framework.ppo.model_arc import PolicyNetwork as PPO

args = {
    # exerpiment details
    "chkpt_dir": "tmp/ppo",
    "log_dir": "run/simple_ppo/",
    # PPO memory
    "total_memory": 25,
    "batch_size": 5,
    # learning hyper parameters actor critic models
    "n_epochs": 3,
    "alpha": 1e-4,
    "gamma": 0.99,
    "gae_lambda": 0.95,
    "policy_clip": 0.2,
    "entropy": 0.01,
    "seed": 7000,
}


class PPOMemory:
    def __init__(self, input_size, batch_size, total_memory):
        self.batch_size = batch_size
        self.total_memory = total_memory
        self.input_size = input_size
        self.clear_memory()

    def generate_batches(self):
        n_states = len(self.observations)
        batch_start = np.arange(0, n_states, self.batch_size)
        indices = np.arange(n_states, dtype=np.int64)
        np.random.shuffle(indices)
        batches = [indices[i : i + self.batch_size] for i in batch_start]

        dic = {
            "observations": self.observations.clone().detach().requires_grad_(True),
            "action": (
                self.actions_move.clone().detach().requires_grad_(True),
                self.probs_move.clone().detach().requires_grad_(True),
                self.actions_communicate.clone().detach().requires_grad_(True),
                self.probs_communicate.clone().detach().requires_grad_(True),
            ),
            "rewards": (
                self.vals.clone().detach().requires_grad_(True),
                self.rewards.clone().detach().requires_grad_(True),
                self.dones.clone().detach().requires_grad_(True),
            ),
            "batches": T.tensor(batches),
        }

        return dic

    def store_memory(
        self,
        observations,
        move,
        probs_move,
        communicate,
        probs_communicate,
        vals,
        reward,
        done,
    ):
        self.observations[self.counter] = observations[0]

        self.actions_move[self.counter] = move
        self.probs_move[self.counter] = probs_move
        self.actions_communicate[self.counter] = communicate
        self.probs_communicate[self.counter] = probs_communicate

        self.vals[self.counter] = vals
        self.rewards[self.counter] = reward
        self.dones[self.counter] = done
        self.counter += 1

    def clear_memory(self):
        self.observations = T.zeros(self.total_memory, self.input_size)

        self.actions_move = T.zeros(self.total_memory, 1)
        self.probs_move = T.zeros(self.total_memory, 1)
        self.actions_communicate = T.zeros(self.total_memory, 1)
        self.probs_communicate = T.zeros(self.total_memory, 1)

        self.vals = T.zeros(self.total_memory)
        self.rewards = T.zeros(self.total_memory)
        self.dones = T.zeros(self.total_memory)
        self.counter = 0


class Agent:
    def __init__(self, args, input_shape, num_layers, num_filters):
        self.gamma = args.gamma
        self.policy_clip = args.policy_clip
        self.n_epochs = args.n_epochs
        self.gae_lambda = args.gae_lambda
        self.entropy = args.entropy

        self.ppo = PPO(input_shape, num_layers, num_filters)
        self.memory = PPOMemory(args)
        self.huber = HuberLoss(reduction="mean", delta=1.0)
        self.device = self.ppo.device

    def remember(
        self,
        observations,
        move,
        probs_move,
        communicate,
        probs_communicate,
        vals,
        reward,
        done,
    ):
        self.memory.store_memory(
            observations,
            move,
            probs_move,
            communicate,
            probs_communicate,
            vals,
            reward,
            done,
        )

    def save_models(self):
        print("... saving models ...")
        self.ppo.save_checkpoint()

    def load_models(self):
        print("... loading models ...")
        self.ppo.load_checkpoint()

    def choose_action(self, observations):

        move, communicate, value = self.ppo(observations)

        move_dist = Categorical(move)
        move_action = move_dist.sample()
        move_probs = move_dist.log_prob(move_action)

        communicate_dist = Categorical(communicate)
        communicate_action = communicate_dist.sample()
        communicate_probs = communicate_dist.log_prob(communicate_action)

        return move_probs, communicate_probs, move_action, communicate_action, value

    def advantage(self, reward_arr, values, dones_arr):
        advantage = np.zeros(len(reward_arr), dtype=np.float16)
        for t in range(len(reward_arr) - 1):
            discount = 1
            a_t = 0
            for k in range(t, len(reward_arr) - 1):
                a_t += discount * (
                    reward_arr[k]
                    + self.gamma * values[k + 1] * (1 - int(dones_arr[k]))
                    - values[k]
                )
                discount *= self.gamma * self.gae_lambda
            advantage[t] = a_t
        advantage = T.tensor(advantage).to(self.device)
        return advantage

    def learn(self):
        for _ in range(self.n_epochs):
            dic = self.memory.generate_batches()

            vals_arr, reward_arr, dones_arr = dic["rewards"]
            (
                actions_move_arr,
                probs_move_arr,
                actions_communicate_arr,
                probs_communicate_arr,
            ) = dic["action"]
            observations_arr = dic["observations"]

            values = vals_arr.to(self.device)

            advantage = self.advantage(reward_arr, values, dones_arr)

            for batch in dic["batches"]:
                total_loss = 0
                #### game inputs/states
                observations = observations_arr[batch].to(self.device)
                adv = advantage[batch]
                #### action
                actions_move = actions_move_arr[batch].to(self.device)
                actions_communicate = actions_communicate_arr[batch].to(self.device)
                old_move_probs = probs_move_arr[batch].to(self.device)
                old_communicate_probs = probs_communicate_arr[batch].to(self.device)
                #### Actor Critic Run
                move, communicate, critic_value = self.ppo(observations)

                #### Critic Loss
                critic_value = T.squeeze(critic_value)
                returns = advantage[batch] + values[batch]
                critic_loss = self.huber(critic_value, returns)
                total_loss += critic_loss * 0.5

                #### Unit, City Actors Loss
                for (dist, actions, old_probs) in [
                    (move, actions_move, old_move_probs),
                    # (communicate, actions_communicate, old_communicate_probs),
                ]:
                    dist = Categorical(dist)
                    new_probs = dist.log_prob(actions)
                    prob_ratio = new_probs.exp() / old_probs.exp()

                    weighted_probs = prob_ratio * adv.reshape(-1, 1)
                    weighted_clipped_probs = T.clamp(
                        prob_ratio, 1 - self.policy_clip, 1 + self.policy_clip
                    ) * adv.reshape(-1, 1)

                    actor_loss = -T.min(weighted_probs, weighted_clipped_probs).mean()

                    total_loss += actor_loss
                    total_loss += self.entropy * T.mean(dist.entropy())

                total_loss.backward()
                self.ppo.optimizer.step()
                self.ppo.optimizer.zero_grad()

        self.memory.clear_memory()


class policy(base_policy):
    def __init__(self, args: Args) -> None:
        self.args = args
        self.agent1 = Agent(self.args)
        self.agent_name = "agent_0"
        self.to_remember = {}
        self.logger = args.logger

        self.added_graph = False

    def action(self, observations):
        obs = observations[self.agent_name]

        obs_batch = T.tensor([obs], dtype=T.float)

        (
            move_probs,
            communicate_probs,
            move_action,
            communicate_action,
            value,
        ) = self.agent1.choose_action(obs_batch)

        if not self.added_graph:
            self.logger.add_graph(self.agent1.ppo, obs_batch)
            self.added_graph = True

        self.to_remember[self.agent_name] = (
            obs_batch,
            move_action,
            move_probs,
            communicate_action,
            communicate_probs,
            value,
        )

        actions = {}

        actions[self.agent_name] = move_action.item()
        print(move_action.item())

        return actions

    def store(self, rewards, dones):

        self.agent1.remember(
            self.to_remember[self.agent_name][0],
            self.to_remember[self.agent_name][1],
            self.to_remember[self.agent_name][2],
            self.to_remember[self.agent_name][3],
            self.to_remember[self.agent_name][4],
            self.to_remember[self.agent_name][5],
            rewards[self.agent_name],
            dones[self.agent_name],
        )

        if self.agent1.memory.counter == self.args.total_memory:
            self.agent1.learn()

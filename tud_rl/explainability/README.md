# Explainability in Deep Reinforcement Learning

This folder contains the helper scripts that were applied in the project.
The applications of the methods during the agent's training are in [train_continuos_explainer.py](../run/train_continuous_explainer.py) and [train_continuos_iPDP.py](../run/train_continuous_iPDP.py).

### Abstract
With the combination of Reinforcement Learning (RL) and Artificial Neural Networks, Deep Reinforcement Learning (DRL) agents are shifted towards being non-interpretable black-box models.
Developers of DRL agents, however, could benefit from enhanced interpretability of the agents' behavior, especially during the training process.
Improved interpretability  could enable developers to make informed adaptations, leading to better overall performance.
The explainability methods Partial Dependence Plot (PDP), Accumulated Local Effects (ALE) and SHapley Additive ex-
Planations (SHAP) were considered to provide insights into how an agent's  behavior evolves during training.
Additionally, a decision tree as a surrogate model was considered to enhance the interpretability of a trained agent.
In a case study, the methods were tested on a Deep Deterministic Policy Gradient (DDPG) agent that was trained in an obstacle avoidance scenario.
PDP, ALE and SHAP were evaluated towards their ability to provide explanations as well as the feasibility of their application in terms of computational overhead.
The decision tree was evaluated towards its ability to approximate the agent's policy as a post-hoc method.
Results demonstrated that PDP, ALE and SHAP were able to provide valuable explanations during the training.
Each method contributed additional information with their individual advantages.
However, the decision tree failed to approximate the agent's actions effectively to be used as a surrogate model.

### Case Study: Obstacle Avoidance
The RL agent was trained in a simple two-dimensional environment consisting of two moving obstacles with a constant distance relative to each other.
The agent's aim was to pass in between the obstacles. [1]

<!-- ![some discription](./img/env.png "some discription")  -->
|<img src="./img/env.png" alt="drawing" width="300"/>|
|:--:|
|*Obstacle avoidance environment taken from [1]*|

The state was represented by six features:
$$s_{t} \coloneq
	\left(
	\begin{array}{c}
		\frac{\ddot{y}_{t,agent}}{a_{y,max}} \\
		\frac{\dot{y}_{t,agent}}{v_{y,max}} \\
		\frac{\dot{x}_{t,agent}-\dot{x_{t,i}}}{v_{x,max}} \\
		\frac{\dot{y}_{t,agent}-\dot{y_{t,i}}}{v_{y,max}} \\
		\frac{x_{t,agent}-x_{t,i}}{x_{scale}} \\
		\frac{y_{t,agent}-y_{t,i}}{y_{scale}}
	\end{array}
	\right)$$


### Bibliography

[1] Hart, Fabian, Martin Waltz and Ostap Okhrin: Missing Velocity in Dynamic Obstacle Avoidance based on Deep Reinforcement Learning. arXiv preprint arXiv:2112.12465, 2021.
from tud_rl.envs._envs.HHOS_Env import *


class HHOS_PathPlanning_Env(HHOS_Env):
    """Does not consider any environmental disturbances since this is considered by the local-path following unit."""
    def __init__(self, 
                 state_design : str, 
                 thrust_control_planner : bool, 
                 data : str, 
                 scenario_based : bool, 
                 N_TSs_max : int, 
                 N_TSs_random : bool, 
                 w_ye : float, 
                 w_ce : float, 
                 w_coll : float, 
                 w_comf : float, 
                 w_speed : float):
        super().__init__(nps_control_follower=None, thrust_control_planner=thrust_control_planner, data=data, scenario_based=scenario_based, w_ye=w_ye, w_ce=w_ce, \
            w_coll=w_coll, w_comf=w_comf, w_speed=w_speed, N_TSs_max=N_TSs_max, N_TSs_random=N_TSs_random)

        assert state_design in ["recursive", "conventional"], "Unknown state design for the HHOS-planner. Should be 'recursive' or 'conventional'."
        self.state_design = state_design

        # forward run
        self.n_loops = int(60.0/self.delta_t)

        # gym inherits
        OS_path_info_size = 3
        self.num_obs_TS = 6
        obs_size = OS_path_info_size + self.lidar_n_beams + self.num_obs_TS * self.N_TSs_max
        act_size = 2 if self.thrust_control_planner else 1

        self.observation_space = spaces.Box(low  = np.full(obs_size, -np.inf, dtype=np.float32), 
                                            high = np.full(obs_size,  np.inf, dtype=np.float32))
        self.action_space = spaces.Box(low  = np.full(act_size, -1, dtype=np.float32), 
                                       high = np.full(act_size,  1, dtype=np.float32))

        # control scales
        self.d_head_scale = dtr(10.0)
        self.surge_scale = 0.5
        self.surge_min = 0.1
        self.surge_max = 5.0
        self.desired_V = 3.0

        self._max_episode_steps = 50


    def reset(self):
        s = super().reset()

        # we can delete the local path and its characteritics
        del self.LocalPath
        del self.loc_ye 
        del self.loc_desired_course
        del self.loc_course_error
        del self.loc_pi_path

        # collision signal
        self.collision_flag = False

        return s

    def _update_local_path(self):
        pass

    def step(self, a, control_TS=True):
        """Takes an action and performs one step in the environment.
        Returns new_state, r, done, {}."""
        # control action
        a = a.flatten()
        self.a = a
        self.OS = self._manual_heading_control(vessel=self.OS, a=float(a[0]))
        if self.thrust_control_planner:
            self.OS = self._manual_surge_control(vessel=self.OS, a=float(a[1]))

        # update agent dynamics (independent of environmental disturbances in this module)
        [self.OS._upd_dynamics() for _ in range(self.n_loops)]

        # environmental effects
        self._update_disturbances()

        # update OS waypoints of global path
        self.OS = self._init_wps(self.OS, "global")

        # compute new cross-track error and course error for global path
        self._set_cte(path_level="global")
        self._set_ce(path_level="global")

        for _ in range(self.n_loops):
            # update TS dynamics (independent of environmental disturbances since they move linear and deterministic)
            [TS._upd_dynamics() for TS in self.TSs]

            # check respawn
            self.TSs = [self._handle_respawn(TS) for TS in self.TSs]

            # update waypoints for other vessels
            self.TSs = [self._init_wps(TS, "global") for TS in self.TSs]

            # simple heading control of target ships
            if control_TS:
                self.TSs = [self._rule_based_control(TS) for TS in self.TSs]

        # increase step cnt and overall simulation time
        self.step_cnt += 1
        self.sim_t += self.n_loops * self.delta_t

        # update collision flag
        self._check_collision()

        # compute state, reward, done        
        self._set_state()
        self._calculate_reward(a)
        d = self._done()
        return self.state, self.r, d, {}

    def _manual_heading_control(self, vessel, a):
        """Adjust the heading of a vessel."""
        assert -1 <= a <= 1, "Unknown action."

        vessel.eta[2] = angle_to_2pi(vessel.eta[2] + a*self.d_head_scale)
        return vessel

    def _manual_surge_control(self, vessel : KVLCC2, a):
        """Adjust the surge of a vessel."""
        assert -1 <= a <= 1, "Unknown action."

        vessel.nu[0] = np.clip(vessel.nu[0] + a*self.surge_scale, self.surge_min, self.surge_max)
        vessel.nps = vessel._get_nps_from_u(vessel.nu[0])
        return vessel

    def _check_collision(self):
        # go ground
        if self.H <= self.OS.critical_depth:
            self.collision_flag = True
            return
        else:
            self.collision_flag = False

        # TS collision
        for TS in self.TSs:
            N0, E0, _ = self.OS.eta
            N1, E1, _ = TS.eta
            
            D = get_ship_domain(A=self.OS.ship_domain_A, B=self.OS.ship_domain_B, C=self.OS.ship_domain_C, D=self.OS.ship_domain_D, OS=self.OS, TS=TS)
            ED_OS_TS = ED(N0=N0, E0=E0, N1=N1, E1=E1, sqrt=True)
            
            if ED_OS_TS <= D:
                self.collision_flag = True
                break

    def _set_state(self):
        #--------------------------- OS information ----------------------------
        # speed, heading relative to global path
        if self.thrust_control_planner:
            state_OS = np.array([self.OS.nu[0]-self.desired_V, angle_to_pi(self.OS.eta[2] - self.glo_pi_path)/math.pi])
        else:
            state_OS = np.array([self.OS.nu[0]/self.desired_V, angle_to_pi(self.OS.eta[2] - self.glo_pi_path)/math.pi])

        # ------------------------- path information ---------------------------
        state_path = np.array([self.glo_ye/self.OS.Lpp])

        # ----------------------- LiDAR for depth -----------------------------
        state_LiDAR = self._get_closeness_from_lidar(self._sense_LiDAR()[0])

        # ----------------------- TS information ------------------------------
        N0, E0, head0 = self.OS.eta
        state_TSs = []

        for TS in self.TSs:
            N, E, headTS = TS.eta

            # closeness
            D = get_ship_domain(A=self.OS.ship_domain_A, B=self.OS.ship_domain_B, C=self.OS.ship_domain_C, D=self.OS.ship_domain_D,\
                 OS=self.OS, TS=TS)
            ED_OS_TS = (ED(N0=N0, E0=E0, N1=N, E1=E, sqrt=True) - D) / (20*self.OS.Lpp)
            closeness = np.clip(1-ED_OS_TS, 0.0, 1.0)

            # relative bearing
            bng_rel_TS = bng_rel(N0=N0, E0=E0, N1=N, E1=E, head0=head0, to_2pi=False) / (math.pi)

            # heading intersection angle with path
            C_TS_path = angle_to_pi(headTS - self.glo_pi_path) / math.pi

            # speed
            if self.thrust_control_planner:
                V_TS = TS._get_V()-self.desired_V
            else:
                V_TS = TS._get_V()/self.desired_V

            # direction
            TS_dir = -1.0 if TS.rev_dir else 1.0

            # speedy
            TS_speedy = 1.0 if TS.speedy else -1.0

            # store it
            state_TSs.append([closeness, bng_rel_TS, C_TS_path, V_TS, TS_dir, TS_speedy])          
        
        if self.state_design == "recursive":
            raise NotImplementedError("Recursive state definition not implemented yet.")

            # no TS is in sight: pad a 'ghost ship' to avoid confusion for the agent
            if len(state_TSs) == 0:
                state_TSs.append([0.0, -1.0, -1.0, 0.0, -1.0, -1.0])

            # sort according to closeness (ascending, larger closeness is more dangerous)
            state_TSs = np.array(sorted(state_TSs, key=lambda x: x[0])).flatten()

            # at least one since there is always the ghost ship
            desired_length = self.num_obs_TS * max([self.N_TSs_max, 1])  

            state_TSs = np.pad(state_TSs, (0, desired_length - len(state_TSs)), \
                'constant', constant_values=np.nan).astype(np.float32)
        
        else:
            # pad ghost ships
            while len(state_TSs) != self.N_TSs_max:
                state_TSs.append([0.0, -1.0, -1.0, 0.0, -1.0, -1.0])

            # sort according to closeness (ascending, larger closeness is more dangerous)
            state_TSs = np.hstack(sorted(state_TSs, key=lambda x: x[0])).astype(np.float32)

        # ------------------------- aggregate information ------------------------
        self.state = np.concatenate([state_OS, state_path, state_LiDAR, state_TSs], dtype=np.float32)


    def _MPC_reward(self):
        """Computes a simplified reward used for solely safety-evaluation."""
        r = 0.0

        # cross-track error
        k_ye = 0.05
        r += math.exp(-k_ye * abs(self.glo_ye))

        # turning around
        if abs(angle_to_pi(self.OS.eta[2] - self.glo_pi_path)) >= math.pi/2:
            r = -100.0

        # collision
        for TS in self.TSs:
            N0, E0, _ = self.OS.eta
            N1, E1, _ = TS.eta

            D = get_ship_domain(A=self.OS.ship_domain_A, B=self.OS.ship_domain_B, C=self.OS.ship_domain_C, D=self.OS.ship_domain_D, OS=self.OS, TS=TS)
            ED_OS_TS = ED(N0=N0, E0=E0, N1=N1, E1=E1, sqrt=True)

            if ED_OS_TS <= 100:
                r -= 100.0
            else:
                r -= math.exp(-(ED_OS_TS-D)/200)

        # hit ground
        if self.H <= self.OS.critical_depth:
            r -= 100.0

        return r


    def _calculate_reward(self, a):
        # ----------------------- GlobalPath-following reward --------------------
        # cross-track error
        k_ye = 0.05
        self.r_ye = math.exp(-k_ye * abs(self.glo_ye))

        # course violation
        #k_ce = 5.0
        if abs(angle_to_pi(self.OS.eta[2] - self.glo_pi_path)) >= math.pi/2:
            self.r_ce = -10.0
        else:
            self.r_ce = 0.0

        # --------------------------- Comfort reward ------------------------
        if self.thrust_control_planner:
            self.r_comf = -float(a[1])**2

        # -------------------------- Speed reward ---------------------------
        if self.thrust_control_planner:
            self.r_speed = max([-(self.OS.nu[0]-self.desired_V)**2, -1.0])

        # ---------------------- Collision Avoidance reward -----------------
        self.r_coll = 0

        # other vessels
        for TS in self.TSs:
            # compute ship domain
            N0, E0, _ = self.OS.eta
            N1, E1, head1 = TS.eta
            D = get_ship_domain(A=self.OS.ship_domain_A, B=self.OS.ship_domain_B, C=self.OS.ship_domain_C, D=self.OS.ship_domain_D, OS=self.OS, TS=TS)
        
            # check if collision
            ED_OS_TS = ED(N0=N0, E0=E0, N1=N1, E1=E1, sqrt=True)
            if ED_OS_TS <= D:
                self.r_coll -= 10.0
            else:
                self.r_coll -= math.exp(-(ED_OS_TS-D)/200)

            #-- violating traffic rules is considered a collision--
            bng_rel_TS_pers = bng_rel(N0=N1, E0=E1, N1=N0, E1=E0, head0=head1)

            # OS should let speedys pass on its portside
            if TS.speedy:
                if dtr(180.0) <= bng_rel_TS_pers <= dtr(270.0) and ED_OS_TS <= 10*self.OS.Lpp:
                    self.r_coll -= 10.0

            # OS should not pass opposing ships on their portside
            elif TS.rev_dir:
                if dtr(0.0) <= bng_rel_TS_pers <= dtr(90.0) and ED_OS_TS <= 10*self.OS.Lpp:
                    self.r_coll -= 10.0

            # normal target ships should be overtaken on their portside
            else:
                if dtr(90.0) <= bng_rel_TS_pers <= dtr(180.0):
                    l = (10 - 5/math.pi * bng_rel_TS_pers)*self.OS.Lpp
                    if ED_OS_TS <= l:
                        self.r_coll -= 10.0

        # hit ground
        if self.H <= self.OS.critical_depth:
            self.r_coll -= 10.0

        # ---------------------------- Aggregation --------------------------
        weights = np.array([self.w_ye, self.w_ce, self.w_coll])
        rews = np.array([self.r_ye, self.r_ce, self.r_coll])

        if self.thrust_control_planner:
            weights = np.append(weights, [self.w_comf, self.w_speed])
            rews = np.append(rews, [self.r_comf, self.r_speed])

        self.r = np.sum(weights * rews) / np.sum(weights) if np.sum(weights) != 0.0 else 0.0


    def _done(self):
        """Returns boolean flag whether episode is over."""
        # OS is too far away from path
        if abs(self.glo_ye) > 1000:
            return True

        # OS reached final waypoint
        elif any([i >= int(0.9*self.n_wps_glo) for i in (self.OS.glo_wp1_idx, self.OS.glo_wp2_idx, self.OS.glo_wp3_idx)]):
            return True

        # artificial done signal
        elif self.step_cnt >= self._max_episode_steps:
            return True
        return False
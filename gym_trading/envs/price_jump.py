from gym import Env, spaces
from data_recorder.database.simulator import Simulator as Sim
from gym_trading.broker import Broker, Order
from gym_trading.render_env import TradingGraph
from configurations.configs import BROKER_FEE
from data_recorder.indicators.rsi import RSI
from data_recorder.indicators.tns import TnS
import logging
import numpy as np


# logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logger = logging.getLogger('PriceJump')


class PriceJump(Env):

    metadata = {'render.modes': ['human']}
    id = 'long-short-v0'
    inventory_features = ['long_inventory', 'short_inventory',
                          'total_unrealized_and_realized_pnl',
                          'long_unrealized_pnl', 'short_unrealized_pnl']
    # Turn to true if Bitifinex is in the dataset (e.g., include_bitfinex=True)
    features = Sim.get_feature_labels(include_system_time=False,
                                      include_bitfinex=False)
    indicator_features = ['tns', 'rsi']
    best_bid_index = features.index('coinbase-bid-distance-0')
    best_ask_index = features.index('coinbase-ask-distance-0')
    notional_bid_index = features.index('coinbase-bid-notional-0')
    notional_ask_index = features.index('coinbase-ask-notional-0')

    buy_trade_index = features.index('coinbase-buys')
    sell_trade_index = features.index('coinbase-sells')

    target_pnl = BROKER_FEE * 10 * 5  # e.g., 5 for max_positions
    fee = BROKER_FEE

    def __init__(self, *,
                 training=True,
                 fitting_file='ETH-USD_2018-12-31.xz',
                 testing_file='ETH-USD_2019-01-01.xz',
                 step_size=1,
                 max_position=5,
                 window_size=10,
                 seed=1,
                 action_repeats=10,
                 format_3d=False):

        # properties required for instantiation
        self.action_repeats = action_repeats
        self._seed = seed
        self._random_state = np.random.RandomState(seed=self._seed)
        self.training = training
        self.step_size = step_size
        self.max_position = max_position
        self.window_size = window_size
        self.format_3d = format_3d  # e.g., [window, features, *NEW_AXIS*]

        self.action = 0
        # derive gym.env properties
        self.actions = np.eye(3)

        self.sym = testing_file[:7]  # slice the CCY from the filename

        # properties that get reset()
        self.reward = 0.0
        self.done = False
        self.local_step_number = 0
        self.midpoint = 0.0
        self.observation = None

        # get Broker class to keep track of PnL and orders
        self.broker = Broker(max_position=max_position)
        # get historical data for simulations
        self.sim = Sim(use_arctic=False)

        fitting_data_filepath = '{}/data_exports/{}'.format(self.sim.cwd,
                                                            fitting_file)
        data_used_in_environment = '{}/data_exports/{}'.format(self.sim.cwd,
                                                               testing_file)
        # print('Fitting data: {}\nTesting Data: {}'.format(fitting_data_filepath,
        #                                                data_used_in_environment))

        fitting_data = self.sim.import_csv(filename=fitting_data_filepath)
        fitting_data['coinbase_midpoint'] = np.log(fitting_data['coinbase_midpoint'].
                                                   values)
        fitting_data['coinbase_midpoint'] = (
                fitting_data['coinbase_midpoint'] -
                fitting_data['coinbase_midpoint'].shift(1)).fillna(method='bfill')
        self.sim.fit_scaler(fitting_data)
        del fitting_data

        self.data = self.sim.import_csv(filename=data_used_in_environment)
        self.prices_ = self.data['coinbase_midpoint'].values  # used to calculate PnL

        self.normalized_data = self.data.copy()
        self.data = self.data.values

        self.max_steps = self.data.shape[0] - self.step_size * \
                         self.action_repeats - 1

        self.normalized_data['coinbase_midpoint'] = \
            np.log(self.normalized_data['coinbase_midpoint'].values)
        self.normalized_data['coinbase_midpoint'] = (
                self.normalized_data['coinbase_midpoint'] -
                self.normalized_data['coinbase_midpoint'].shift(1)).fillna(method='bfill')

        self.tns = TnS()
        self.rsi = RSI()

        logger.info("Pre-scaling {}-{} data...".format(self.sym, self._seed))
        self.normalized_data = self.normalized_data.apply(self.sim.z_score, axis=1).values
        logger.info("...{}-{} pre-scaling complete.".format(self.sym, self._seed))

        # rendering class
        self._render = TradingGraph(sym=self.sym)
        # graph midpoint prices
        self._render.reset_render_data(
            y_vec=self.prices_[:np.shape(self._render.x_vec)[0]])

        self.data_buffer = list()

        self.action_space = spaces.Discrete(len(self.actions))

        variable_features_count = len(self.inventory_features) + len(self.actions) + 1 + \
                                  len(PriceJump.indicator_features)

        if self.format_3d:
            shape = (self.window_size,
                     len(PriceJump.features) + variable_features_count,
                     1)
        else:
            shape = (self.window_size,
                     len(PriceJump.features) + variable_features_count)

        self.observation_space = spaces.Box(low=self.data.min(),
                                            high=self.data.max(),
                                            shape=shape,
                                            dtype=np.int)

        print('PriceJump #{} instantiated.\nself.observation_space.shape : {}'.format(
            self._seed, self.observation_space.shape))

    def __str__(self):
        return '{} | {}-{}'.format(PriceJump.id, self.sym, self._seed)

    def step(self, action):

        for current_step in range(self.action_repeats):

            if self.done:
                self.reset()
                return self.observation, self.reward, self.done

            # reset the reward if there ARE action repeats
            if current_step == 0:
                self.reward = 0.
                step_action = action
            else:
                step_action = 0

            # Get current step's midpoint
            self.midpoint = self.prices_[self.local_step_number]
            # Pass current time step midpoint to broker to calculate PnL,
            # or if any open orders are to be filled
            buy_volume = self._get_book_data(PriceJump.buy_trade_index)
            sell_volume = self._get_book_data(PriceJump.sell_trade_index)

            self.tns.step(buys=buy_volume, sells=sell_volume)
            self.rsi.step(price=self.midpoint)

            self.broker.step(midpoint=self.midpoint)

            self.reward += self._send_to_broker_and_get_reward(action=step_action)

            step_position_features = self._create_position_features()
            step_action_features = self._create_action_features(action=step_action)
            step_indicator_features = self._create_indicator_features()

            step_observation = np.concatenate((
                self.process_data(self.normalized_data[self.local_step_number]),
                step_indicator_features,
                step_position_features,
                step_action_features,
                np.array([self.reward], dtype=np.float32)),
                axis=None)
            self.data_buffer.append(step_observation)

            if len(self.data_buffer) > self.window_size:
                del self.data_buffer[0]

            self.local_step_number += self.step_size

        self.observation = np.array(self.data_buffer, dtype=np.float32)

        # Expand the observation space from 2 to 3 dimensions.
        # This is necessary for conv nets in Baselines.
        if self.format_3d:
            self.observation = np.expand_dims(self.observation, axis=-1)

        if self.local_step_number > self.max_steps:
            self.done = True
            order = Order(ccy=self.sym, side=None, price=self.midpoint,
                          step=self.local_step_number)
            self.reward = self.broker.flatten_inventory(order=order)

        return self.observation, self.reward, self.done, {}

    def reset(self):
        if self.training:
            self.local_step_number = self._random_state.randint(
                low=1,
                high=self.data.shape[0] // 4)
        else:
            self.local_step_number = 0

        msg = ' {}-{} reset. Episode pnl: {:.4f} with {} trades | First step: {}'.format(
            self.sym, self._seed,
            self.broker.get_total_pnl(midpoint=self.midpoint),
            self.broker.get_total_trade_count(),
            self.local_step_number
        )
        logger.info(msg)
        self.reward = 0.0
        self.done = False
        self.broker.reset()
        self.data_buffer.clear()
        self.rsi.reset()
        self.tns.reset()

        for step in range(self.window_size + self.tns.window):

            self.midpoint = self.prices_[self.local_step_number]

            step_buy_volume = self._get_book_data(PriceJump.buy_trade_index)
            step_sell_volume = self._get_book_data(PriceJump.sell_trade_index)

            self.tns.step(buys=step_buy_volume, sells=step_sell_volume)
            self.rsi.step(price=self.midpoint)

            step_position_features = self._create_position_features()
            step_action_features = self._create_action_features(action=0)
            step_indicator_features = self._create_indicator_features()

            step_observation = np.concatenate((self.process_data(
                self.normalized_data[self.local_step_number]),
                                               step_indicator_features,
                                               step_position_features,
                                               step_action_features,
                                               np.array([self.reward])),
                axis=None)
            self.data_buffer.append(step_observation)
            self.local_step_number += self.step_size

            if len(self.data_buffer) > self.window_size:
                del self.data_buffer[0]

        self.observation = np.array(self.data_buffer, dtype=np.float32)

        # Expand the observation space from 2 to 3 dimensions.
        # This is necessary for conv nets in Baselines.
        if self.format_3d:
            self.observation = np.expand_dims(self.observation, axis=-1)

        return self.observation

    def render(self, mode='human'):
        self._render.render(midpoint=self.midpoint, mode=mode)

    def close(self):
        logger.info('{}-{} is being closed.'.format(self.id, self.sym))
        self.data = None
        self.normalized_data = None
        self.prices_ = None
        self.broker = None
        self.sim = None
        self.data_buffer = None
        self.tns = None
        self.rsi = None
        return

    def seed(self, seed=1):
        self._random_state = np.random.RandomState(seed=seed)
        self._seed = seed
        logger.info('PriceJump.seed({})'.format(seed))
        return [seed]

    @staticmethod
    def process_data(_next_state):
        return np.clip(_next_state.reshape((1, -1)), -10., 10.)

    # def process_data(self, _next_state):
    #     # return self.sim.scale_state(_next_state).values.reshape((1, -1))
    #     return np.reshape(_next_state, (1, -1))

    def _send_to_broker_and_get_reward(self, action):
        reward = 0.0
        discouragement = 0.000000000001

        if action == 0:  # do nothing
            reward += discouragement

        elif action == 1:  # buy
            price_fee_adjusted = self.midpoint + (PriceJump.fee * self.midpoint)
            if self.broker.short_inventory_count > 0:
                order = Order(ccy=self.sym, side='short',
                              price=price_fee_adjusted,
                              step=self.local_step_number)
                self.broker.remove(order=order)
                reward += self.broker.get_reward(side=order.side)

            elif self.broker.long_inventory_count >= 0:
                order = Order(ccy=self.sym, side='long',
                              price=price_fee_adjusted,
                              step=self.local_step_number)
                if self.broker.add(order=order) is False:
                    reward -= discouragement

            else:
                logger.info(('gym_trading.get_reward() ' +
                             'Error for action #{} - ' +
                             'unable to place an order with broker').format(action))

        elif action == 2:  # sell
            price_fee_adjusted = self.midpoint - (PriceJump.fee * self.midpoint)
            if self.broker.long_inventory_count > 0:
                order = Order(ccy=self.sym, side='long',
                              price=price_fee_adjusted,
                              step=self.local_step_number)
                self.broker.remove(order=order)
                reward += self.broker.get_reward(side=order.side)
            elif self.broker.short_inventory_count >= 0:
                order = Order(ccy=self.sym, side='short',
                              price=price_fee_adjusted,
                              step=self.local_step_number)
                if self.broker.add(order=order) is False:
                    reward -= discouragement

            else:
                logger.info('gym_trading.get_reward() ' +
                            'Error for action #{} - ' +
                            'unable to place an order with broker'
                            .format(action))

        else:
            logger.info(('Unknown action to take in get_reward(): ' +
                         'action={} | midpoint={}').format(action, self.midpoint))

        return reward

    def _create_position_features(self):
        return np.array(
            (self.broker.long_inventory.position_count / self.max_position,
             self.broker.short_inventory.position_count / self.max_position,
             self.broker.get_total_pnl(midpoint=self.midpoint) / PriceJump.target_pnl,
             self.broker.long_inventory.get_unrealized_pnl(self.midpoint) /
                self.broker.reward_scale,
             self.broker.short_inventory.get_unrealized_pnl(self.midpoint) /
                self.broker.reward_scale),
            dtype=np.float32)

    def _create_action_features(self, action):
        return self.actions[action]

    def _create_indicator_features(self):
        return np.array((self.tns.get_value(), self.rsi.get_value()), dtype=np.float32)

    def _get_nbbo(self):
        best_bid = round(self.midpoint - self._get_book_data(
            PriceJump.best_bid_index), 2)
        best_ask = round(self.midpoint + self._get_book_data(
            PriceJump.best_ask_index), 2)
        return best_bid, best_ask

    def _get_book_data(self, index=0):
        return self.data[self.local_step_number][index]

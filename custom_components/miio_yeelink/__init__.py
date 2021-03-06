"""Support for Yeelink."""
import logging
from math import ceil
from datetime import timedelta
from functools import partial
import voluptuous as vol

from homeassistant import core, config_entries
from homeassistant.const import *
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.config_validation as cv

from homeassistant.components.light import *
from homeassistant.components.fan import *

from miio import (
    Device,
    Yeelight,
    DeviceException,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'miio_yeelink'
SCAN_INTERVAL = timedelta(seconds = 60)
DEFAULT_NAME = 'Xiaomi Yeelink'
CONF_MODEL = 'model'
SPEED_FIERCE = 'fierce'

CCT_MIN = 1
CCT_MAX = 100

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_TOKEN): vol.All(cv.string, vol.Length(min=32, max=32)),
        vol.Optional(CONF_NAME, default = DEFAULT_NAME): cv.string,
        vol.Optional(CONF_MODEL, default = ''): cv.string,
        vol.Optional(CONF_MODE, default = ''): cv.string,
    }
)

XIAOMI_MIIO_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    },
)

LIGHT_SCENES = [
    None,
    ['cf',2,1,'50,2,4000,1,900000,2,4000,100'],
    ['cf',2,2,'50,2,4000,50,600000,2,4000,1'],
    ['nightlight',30],
    ['ct',4000,50],
    ['nightlight',50],
    ['ct',4000,100],
    ['cf',6,0,'600,2,4000,70,400,2,4000,1'],
    ['ct',5000,100],
]


async def async_setup(hass, config: dict):
    hass.data.setdefault(DOMAIN, {})
    component = EntityComponent(_LOGGER, DOMAIN, hass, SCAN_INTERVAL)
    hass.data[DOMAIN]['component'] = component
    await component.async_setup(config)
    component.async_register_entity_service(
        'send_command',
        XIAOMI_MIIO_SERVICE_SCHEMA.extend(
            {
                vol.Required('method'): cv.string,
                vol.Optional('params', default = []): cv.ensure_list,
            },
        ),
        'async_command'
    )
    return True

async def async_setup_entry(hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry):
    hass.data[DOMAIN].setdefault('configs', {})
    entry_id = config_entry.entry_id
    unique_id = config_entry.unique_id
    info = config_entry.data.get('miio_info') or {}
    platforms = ['light', 'fan']
    plats = []
    config = {}
    for k in [CONF_HOST, CONF_TOKEN, CONF_NAME, CONF_MODE, CONF_MODE]:
        config[k] = config_entry.data.get(k)
    model = config.get(CONF_MODEL) or info.get(CONF_MODEL) or ''
    config[CONF_MODEL] = model
    mode = config.get(CONF_MODE) or ''
    for m in mode.split(','):
        if m in platforms:
            plats.append(m)
            config[CONF_MODE] = ''
    if not plats:
        if model.find('bhf_light') > 0 or model.find('fancl') > 0:
            plats = platforms
        elif model.find('ceiling') > 0 or model.find('panel') > 0:
            plats = ['light']
        elif model.find('ven_fan') > 0:
            plats = ['fan']
        else:
            plats = platforms
    hass.data[DOMAIN]['configs'][unique_id] = config
    _LOGGER.debug('Yeelink async_setup_entry %s', {
        'entry_id' : entry_id,
        'unique_id' : unique_id,
        'config' : config,
        'plats' : plats,
        'miio' : info,
    })
    for plat in plats:
        hass.async_create_task(hass.config_entries.async_forward_entry_setup(config_entry, plat))
    return True


class MiotDevice(Device):
    def __init__(
        self,
        mapping: dict,
        ip: str = None,
        token: str = None,
        start_id: int = 0,
        debug: int = 0,
        lazy_discover: bool = True,
    ) -> None:
        self.mapping = mapping
        super().__init__(ip, token, start_id, debug, lazy_discover)

    def get_properties_for_mapping(self) -> list:
        properties = [{'did': k, **v} for k, v in self.mapping.items()]
        return self.get_properties(properties, property_getter = 'get_properties', max_properties = 15)

    def set_property(self, property_key: str, value):
        pms = self.mapping[property_key]
        _LOGGER.debug('Set properties command for %s: %s(%s) %s', self.ip, property_key, value, pms)
        return self.send('set_properties',[{'did': property_key, **pms, 'value': value}])


class MiioEntity(ToggleEntity):
    def __init__(self, name, device):
        self._device = device
        self._miio_info = device.info()
        self._unique_did = dr.format_mac(self._miio_info.mac_address)
        self._unique_id = self._unique_did
        self._name = name
        self._model = self._miio_info.model or ''
        self._state = None
        self._available = False
        self._state_attrs = {
            CONF_MODEL: self._model,
            'lan_ip': self._miio_info.network_interface.get('localIp'),
            'mac_address': self._miio_info.mac_address,
            'firmware_version': self._miio_info.firmware_version,
            'hardware_version': self._miio_info.hardware_version,
            'entity_class': self.__class__.__name__,
        }
        self._supported_features = 0
        self._props = ['power']
        self._success_result = ['ok']

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return self._name

    @property
    def available(self):
        return self._available

    @property
    def is_on(self):
        return self._state

    @property
    def device_state_attributes(self):
        return self._state_attrs

    @property
    def supported_features(self):
        return self._supported_features

    @property
    def device_info(self):
        return {
            'identifiers': {(DOMAIN, self._unique_did)},
            'name': self._name,
            'model': self._model,
            'manufacturer': (self._model or 'Xiaomi').split('.',1)[0],
            'sw_version': self._miio_info.firmware_version,
        }

    async def _try_command(self, mask_error, func, *args, **kwargs):
        try:
            result = await self.hass.async_add_executor_job(partial(func, *args, **kwargs))
            _LOGGER.debug('Response received from %s: %s', self._name, result)
            return result == self._success_result
        except DeviceException as exc:
            if self._available:
                _LOGGER.error(mask_error, exc)
                self._available = False
            return False

    async def async_command(self, method, params = [], mask_error = None):
        _LOGGER.debug('Send miio command to %s: %s(%s)', self._name, method, params)
        if mask_error is None:
            mask_error = f'Send miio command to {self._name}: {method} failed: %s'
        result = await self._try_command(mask_error, self._device.send, method, params)
        if result == False:
            _LOGGER.info('Send miio command to %s failed: %s(%s)', self._name, method, params)
        return result

    async def async_update(self):
        try:
            attrs = await self.hass.async_add_executor_job(partial(self._device.get_properties, self._props))
        except DeviceException as ex:
            if self._available:
                self._available = False
                _LOGGER.error('Got exception while fetching the state for %s: %s', self._name, ex)
            return
        attrs = dict(zip(self._props, attrs))
        _LOGGER.debug('Got new state from %s: %s', self._name, attrs)
        self._available = True
        self._state = attrs.get('power') == 'on'
        self._state_attrs.update(attrs)

    async def async_turn_on(self, **kwargs):
        await self._try_command('Turning on failed.', self._device.on)

    async def async_turn_off(self, **kwargs):
        await self._try_command('Turning off failed.', self._device.off)


class MiotEntity(MiioEntity):
    def __init__(self, name, device):
        super().__init__(name, device)
        self._success_result = 0

    async def _try_command(self, mask_error, func, *args, **kwargs):
        try:
            results = await self.hass.async_add_executor_job(partial(func, *args, **kwargs))
            for result in results:
                break
            _LOGGER.debug('Response received from miot %s: %s', self._name, result)
            return result.get('code',1) == self._success_result
        except DeviceException as exc:
            if self._available:
                _LOGGER.error(mask_error, exc)
                self._available = False
            return False

    async def async_command(self, method, params = [], mask_error = None):
        _LOGGER.debug('Send miot command to %s: %s(%s)', self._name, method, params)
        if mask_error is None:
            mask_error = f'Send miot command to {self._name}: {method} failed: %s'
        result = await self._try_command(mask_error, self._device.send, method, params)
        if result == False:
            _LOGGER.info('Send miot command to %s failed: %s(%s)', self._name, method, params)
        return result

    async def async_update(self):
        try:
            results = await self.hass.async_add_executor_job(partial(self._device.get_properties_for_mapping))
        except DeviceException as ex:
            if self._available:
                self._available = False
                _LOGGER.error('Got exception while fetching the state for %s: %s', self._name, ex)
            return
        attrs = {
            prop['did']: prop['value'] if prop['code'] == 0 else None
            for prop in results
        }
        _LOGGER.debug('Got new state from %s: %s', self._name, attrs)
        self._available = True
        self._state = True if attrs.get('power') else False
        self._state_attrs.update(attrs)

    async def async_set_property(self, field, value):
        return await self._try_command(
            f'Miot set_property failed. {field}: {value} %s',
            self._device.set_property,
            field,
            value,
        )

    async def async_turn_on(self, **kwargs):
        await self.async_set_property('power', True)

    async def async_turn_off(self, **kwargs):
        await self.async_set_property('power', False)


class YeelightEntity(MiioEntity, LightEntity):
    def __init__(self, config):
        name  = config[CONF_NAME]
        host  = config[CONF_HOST]
        token = config[CONF_TOKEN]
        model = config.get(CONF_MODEL)
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        self._device = Yeelight(host, token)
        super().__init__(name, self._device)
        self._unique_id = f'{self._miio_info.model}-{self._miio_info.mac_address}-light'

        self._supported_features = SUPPORT_BRIGHTNESS | SUPPORT_COLOR_TEMP
        if self._model in ['yeelink.bhf_light.v2']:
            self._supported_features = SUPPORT_BRIGHTNESS

        self._props = ['power','nl_br','delayoff']
        if self.supported_features & SUPPORT_BRIGHTNESS:
            self._props.append('bright')
        if self.supported_features & SUPPORT_COLOR_TEMP:
            self._props.append('ct')

        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._brightness = None
        self._color_temp = None
        self._delay_off = None
        self._scenes = LIGHT_SCENES

    @property
    def brightness(self):
        return self._brightness

    @property
    def color_temp(self):
        return self._color_temp

    @property
    def min_mireds(self):
        return 2700

    @property
    def max_mireds(self):
        num = 5700
        if self._model in ['yeelink.light.ceiling18','YLXD56YL','YLXD53YL']:
            num = 6500
        elif self._model in ['yeelink.light.ceiling21','MJXDD02YL']:
            num = 6300
        elif self._model in ['yeelink.light.ceiling22','MJXDD01SYL','yeelink.light.ceiling23','MJXDD03SYL']:
            num = 6000
        return num

    @property
    def delay_off(self):
        return self._delay_off

    async def async_update(self):
        await super().async_update()
        if self._available:
            attrs = self._state_attrs
            if self.supported_features & SUPPORT_BRIGHTNESS and 'bright' in attrs:
                self._brightness = ceil(255 / 100 * int(attrs.get('bright',0)))
            if self.supported_features & SUPPORT_COLOR_TEMP and 'ct' in attrs:
                self._color_temp = int(attrs.get('ct',0))
            if 'delayoff' in attrs:
                self._delay_off  = int(attrs.get('delayoff',0))

    async def async_turn_on(self, **kwargs):
        if self.supported_features & SUPPORT_COLOR_TEMP and ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs[ATTR_COLOR_TEMP]
            percent_color_temp = self.translate_color_temp(
                color_temp, self.min_mireds, self.max_mireds, CCT_MIN, CCT_MAX
            )
            _LOGGER.debug('Setting color temperature: %s mireds, %s%% cct', color_temp, percent_color_temp)
            result = await self._try_command(
                'Setting color temperature failed: %s mireds',
                self._device.set_color_temp,
                color_temp,
            )
            if result:
                self._color_temp = color_temp

        if self.supported_features & SUPPORT_BRIGHTNESS and ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            percent_brightness = ceil(100 * brightness / 255)
            _LOGGER.debug('Setting brightness: %s %s%%', brightness, percent_brightness)
            result = await self._try_command(
                'Setting brightness failed: %s',
                self._device.set_brightness,
                percent_brightness,
            )
            if result:
                self._brightness = brightness

        if self._state != True:
            await self._try_command('Turning the light on failed.', self._device.on)

    async def async_set_scene(self, scene = 0, params = None):
        _LOGGER.debug('Setting scene: %s params: %s', scene, params)
        pms = None
        if params is None:
            pms = self._scenes[scene]
        else:
            pms = params
        if pms is None:
            _LOGGER.error('Error params for set_scene: %s params: %s', scene, params)
        else:
            await self.async_command('set_scene', pms)

    async def async_set_delayed_turn_off(self, time_period: timedelta, power = False):
        _LOGGER.debug('Setting delayed_turn_off: %s power: %s', time_period, power)
        if power:
            await self.async_turn_on()
        await self.async_command(
            'cron_add',
            [0, time_period.total_seconds() // 60],
            'Setting the turn off delay failed: %s',
        )

    @staticmethod
    def translate_color_temp(value, left_min, left_max, right_min, right_max):
        left_span = left_max - left_min
        right_span = right_max - right_min
        value_scaled = float(value - left_min) / float(left_span)
        return int(right_min + (value_scaled * right_span))


class BathHeaterEntity(MiioEntity, FanEntity):
    def __init__(self, config, mode = 'warmwind'):
        name  = config[CONF_NAME]
        host  = config[CONF_HOST]
        token = config[CONF_TOKEN]
        model = config.get(CONF_MODEL)
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        self._device = Device(host, token)
        super().__init__(name, self._device)
        self._unique_id = f'{self._miio_info.model}-{self._miio_info.mac_address}-{mode}'
        self._mode = mode
        self._supported_features = SUPPORT_SET_SPEED
        self._props = ['power','bright','delayoff','nl_br','nighttime','bh_mode','bh_delayoff','light_mode','fan_speed_idx']
        self._state_attrs.update({
            'mode': mode,
            'entity_class': self.__class__.__name__,
        })
        self._mode_speeds = {}

    @property
    def mode(self):
        return self._mode

    async def async_update(self):
        await super().async_update()
        if self._available:
            attrs = self._state_attrs
            self._state = attrs.get('bh_mode') == self._mode
            if 'fan_speed_idx' in attrs:
                fls = '%05d' % int(attrs.get('fan_speed_idx',0))
                self._mode_speeds = {
                    'warmwind'     : fls[4],
                    'venting'      : fls[3],
                    'drying'       : fls[2],
                    'drying_cloth' : fls[0],
                    'coolwind'     : fls[1],
                }

    async def async_turn_on(self, speed: Optional[str] = None, **kwargs):
        _LOGGER.debug('Turning on for %s. speed: %s %s', self._name, speed, kwargs)
        if speed == SPEED_OFF:
            await self.async_turn_off()
        else:
            if self._state != True:
                result = await self.async_command('set_bh_mode', [self._mode])
                if result:
                    self._state = True
            if speed:
                await self.async_set_speed(speed)

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug('Turning off for %s.', self._name)
        result = await self.async_command('stop_bath_heater')
        if result:
            self._state = False

    @property
    def speed(self):
        spd = int(self._mode_speeds.get(self._mode,0))
        if spd >= 3:
            return SPEED_FIERCE
        elif spd >= 2:
            return SPEED_HIGH
        elif spd >= 1:
            return SPEED_MEDIUM
        return SPEED_LOW

    @property
    def mode_speeds(self):
        return self._mode_speeds

    @property
    def speed_list(self):
        fls = [SPEED_LOW, SPEED_MEDIUM, SPEED_HIGH]
        if self._mode == 'venting':
            fls.append(SPEED_FIERCE)
        return fls

    @staticmethod
    def speed_to_gears(speed = None):
        spd = 0
        if speed == SPEED_MEDIUM:
            spd = 1
        if speed == SPEED_HIGH:
            spd = 2
        if speed == SPEED_FIERCE:
            spd = 9
        return spd


    async def async_set_speed(self, speed: Optional[str] = None):
        spd = self.speed_to_gears(speed)
        _LOGGER.debug('Setting speed for %s: %s(%s)', self._name, speed, spd)
        result = await self.async_command('set_gears_idx', [spd])
        if result:
            self._mode_speeds.update({
                self._mode: spd,
            })
            if 'gears' in self._state_attrs:
                self._state_attrs.update({
                    'gears': spd,
                })


class BathHeaterEntityV5(BathHeaterEntity):
    def __init__(self, config, mode = 'warmwind'):
        super().__init__(config, mode)
        self._supported_features = SUPPORT_SET_SPEED | SUPPORT_OSCILLATE | SUPPORT_DIRECTION
        self._props = ['power','bh_mode','fan_speed_idx','swing_action','swing_angle','bh_cfg_delayoff','bh_delayoff','light_cfg_delayoff','delayoff','aim_temp']
        self._state_attrs.update({'entity_class': self.__class__.__name__})

    async def async_update(self):
        await super().async_update()
        if self._available:
            attrs = self._state_attrs
            mode = attrs.get('bh_mode') or ''
            self._state = self._mode == mode or self._mode in mode.split('|')
            if 'fan_speed_idx' in attrs:
                fls = '%03d' % int(attrs.get('fan_speed_idx',0))
                self._mode_speeds = {
                    'warmwind' : fls[0],
                    'coolwind' : fls[1],
                    'venting'  : fls[2],
                }

    async def async_turn_on(self, speed: Optional[str] = None, **kwargs):
        _LOGGER.debug('Turning on for %s. speed: %s %s', self._name, speed, kwargs)
        if speed == SPEED_OFF:
            await self.async_turn_off()
        else:
            hasSpeed = self._mode in self._mode_speeds
            spd = self.speed_to_gears(speed or SPEED_HIGH) if hasSpeed else 0
            result = await self.async_command('set_bh_mode', [self._mode, spd])
            if result:
                self._state = True
                if hasSpeed:
                    self._mode_speeds.update({
                        self._mode: spd,
                    })

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug('Turning off for %s.', self._name)
        result = await self.async_command('set_bh_mode', ['bh_off', 0])
        if result:
            self._state = False

    @property
    def speed(self):
        spd = int(self._mode_speeds.get(self._mode,0))
        if spd >= 2:
            return SPEED_HIGH
        return SPEED_LOW

    @property
    def mode_speeds(self):
        return self._mode_speeds

    @property
    def speed_list(self):
        fls = [SPEED_LOW, SPEED_HIGH]
        return fls

    @staticmethod
    def speed_to_gears(speed = None):
        spd = 0
        if speed == SPEED_LOW:
            spd = 1
        if speed == SPEED_HIGH:
            spd = 2 if self._mode == 'warmwind' else 3
        return spd


    async def async_set_speed(self, speed: Optional[str] = None):
        await self.async_turn_on(speed)

    @property
    def oscillating(self):
        return self._state_attrs.get('swing_action') == 'swing'

    async def async_oscillate(self, oscillating: bool):
        act = 'swing' if oscillating else 'stop'
        _LOGGER.debug('Setting oscillating for %s: %s(%s)', self._name, act, oscillating)
        result = await self.async_command('set_swing', [act, 0])
        if result:
            self._state_attrs.update({
                'swing_action': act,
            })

    @property
    def current_direction(self):
        if int(self._state_attrs.get('swing_angle',0)) > 90:
            return DIRECTION_REVERSE
        return DIRECTION_FORWARD

    async def async_set_direction(self, direction: str):
        act = 'angle'
        try:
            num = int(direction)
        except:
            num = 0
        if num < 1:
            num = 120 if direction == DIRECTION_REVERSE else 90
        _LOGGER.debug('Setting direction for %s: %s(%s)', self._name, direction, num)
        result = await self.async_command('set_swing', [act, num])
        if result:
            self._state_attrs.update({
                'swing_action': act,
                'swing_angle':  num,
            })


class VenFanEntity(BathHeaterEntity):
    def __init__(self, config, mode = 'coolwind'):
        super().__init__(config, mode)
        self._supported_features = SUPPORT_SET_SPEED | SUPPORT_OSCILLATE | SUPPORT_DIRECTION
        self._props = ['bh_mode','gears','swing_action','swing_angle','bh_delayoff','anion_onoff','init_fan_opt']
        self._state_attrs.update({'entity_class': self.__class__.__name__})

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug('Turning off for %s.', self._name)
        result = await self.async_command('set_bh_mode', ['bh_off'])
        if result:
            self._state = False

    @property
    def speed(self):
        if int(self._state_attrs.get('gears', 0)) >= 1:
            return SPEED_HIGH
        return SPEED_LOW

    @property
    def speed_list(self):
        return [SPEED_LOW, SPEED_HIGH]

    @staticmethod
    def speed_to_gears(speed = None):
        spd = 0
        if speed == SPEED_HIGH:
            spd = 1
        return spd

    @property
    def oscillating(self):
        return self._state_attrs.get('swing_action') == 'swing'

    async def async_oscillate(self, oscillating: bool):
        act = 'swing' if oscillating else 'stop'
        _LOGGER.debug('Setting oscillating for %s: %s(%s)', self._name, act, oscillating)
        result = await self.async_command('set_swing', [act, 0])
        if result:
            self._state_attrs.update({
                'swing_action': act,
            })

    @property
    def current_direction(self):
        if int(self._state_attrs.get('swing_angle',0)) > 90:
            return DIRECTION_REVERSE
        return DIRECTION_FORWARD

    async def async_set_direction(self, direction: str):
        act = 'angle'
        try:
            num = int(direction)
        except:
            num = 0
        if num < 1:
            num = 120 if direction == DIRECTION_REVERSE else 90
        _LOGGER.debug('Setting direction for %s: %s(%s)', self._name, direction, num)
        result = await self.async_command('set_swing', [act, num])
        if result:
            self._state_attrs.update({
                'swing_action': act,
                'swing_angle':  num,
            })


class MiotLightEntity(MiotEntity, LightEntity):
    mapping = {
        # http://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:light:0000A001:yeelink-fancl1:1
        'power'    : {'siid':2, 'piid':1},
        'mode'     : {'siid':2, 'piid':2},
        'bright'   : {'siid':2, 'piid':3},
        'ct'       : {'siid':2, 'piid':5},
        'flow'     : {'siid':2, 'piid':6},
        'delayoff' : {'siid':2, 'piid':7},
        'init_opt' : {'siid':4, 'piid':1},
        'nighttime': {'siid':4, 'piid':2},
    }
    def __init__(self, config):
        name  = config[CONF_NAME]
        host  = config[CONF_HOST]
        token = config[CONF_TOKEN]
        model = config.get(CONF_MODEL)
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        if model in ['yeelink.light.fancl1','YLFD02YL']:
            self.mapping.update({
                'scenes' : {'siid':4, 'piid':3},
            })

        self._device = MiotDevice(self.mapping, host, token)
        super().__init__(name, self._device)

        self._supported_features = SUPPORT_BRIGHTNESS | SUPPORT_COLOR_TEMP
        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._brightness = None
        self._color_temp = None
        self._delay_off = None
        self._scenes = LIGHT_SCENES

    @property
    def brightness(self):
        return self._brightness

    @property
    def color_temp(self):
        return self._color_temp

    @property
    def min_mireds(self):
        return 2700

    @property
    def max_mireds(self):
        num = 5700
        if self._model in ['yeelink.light.fancl1','YLFD02YL','yeelink.light.fancl2','YLFD001']:
            num = 6500
        return num

    @property
    def delay_off(self):
        return self._delay_off

    async def async_update(self):
        await super().async_update()
        if self._available:
            attrs = self._state_attrs
            if self.supported_features & SUPPORT_BRIGHTNESS and 'bright' in attrs:
                self._brightness = ceil(255 / 100 * int(attrs.get('bright',0)))
            if self.supported_features & SUPPORT_COLOR_TEMP and 'ct' in attrs:
                self._color_temp = int(attrs.get('ct',0))
            if 'delayoff' in attrs:
                self._delay_off  = int(attrs.get('delayoff',0))

    async def async_turn_on(self, **kwargs):
        if self.supported_features & SUPPORT_COLOR_TEMP and ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs[ATTR_COLOR_TEMP]
            _LOGGER.debug('Setting color temperature: %s mireds', color_temp)
            result = await self.async_set_property('ct', color_temp)
            if result:
                self._color_temp = color_temp

        if self.supported_features & SUPPORT_BRIGHTNESS and ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            percent_brightness = ceil(100 * brightness / 255)
            _LOGGER.debug('Setting brightness: %s %s%%', brightness, percent_brightness)
            result = await self.async_set_property('bright', percent_brightness)
            if result:
                self._brightness = brightness

        if self._state != True:
            await super().async_turn_on()

    async def async_set_scene(self, scene = 0, params = None):
        _LOGGER.debug('Setting scene: %s params: %s', scene, params)
        pms = None
        if params is None:
            pms = self._scenes[scene]
        else:
            pms = params
        if pms is None:
            _LOGGER.error('Error params for scene: %s params: %s', scene, params)
        else:
            await self.async_command('scene', pms)

    async def async_set_delayed_turn_off(self, time_period: timedelta, power = False):
        _LOGGER.debug('Setting delayed_turn_off: %s power: %s', time_period, power)
        if power:
            await self.async_turn_on()
        await self.async_command(
            'delayoff',
            [0, time_period.total_seconds() // 60],
            'Setting the turn off delay failed: %s',
        )


class MiotFanEntity(MiotEntity, FanEntity):
    mapping = {
        # http://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:light:0000A001:yeelink-fancl1:1
        'power'    : {'siid':3, 'piid':1},
        'gears'    : {'siid':3, 'piid':2},
        'mode'     : {'siid':3, 'piid':7},
        'status'   : {'siid':3, 'piid':8},
        'fault'    : {'siid':3, 'piid':9},
        'dalayoff' : {'siid':3, 'piid':10},
        'init_opt' : {'siid':5, 'piid':1},
    }
    def __init__(self, config):
        name  = config[CONF_NAME]
        host  = config[CONF_HOST]
        token = config[CONF_TOKEN]
        model = config.get(CONF_MODEL)
        _LOGGER.info('Initializing with host %s (token %s...)', host, token[:5])

        if model in ['yeelink.light.fancl2','YLFD001']:
            # http://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:light:0000A001:yeelink-fancl2:1
            self.mapping.update({
                'dalayoff' : {'siid':3, 'piid':11},
            })

        self._device = MiotDevice(self.mapping, host, token)
        super().__init__(name, self._device)
        self._unique_id = f'{self._miio_info.model}-{self._miio_info.mac_address}-fan'
        self._supported_features = SUPPORT_SET_SPEED
        self._state_attrs.update({'entity_class': self.__class__.__name__})

    async def async_turn_on(self, speed: Optional[str] = None, **kwargs):
        _LOGGER.debug('Turning on for %s. speed: %s %s', self._name, speed, kwargs)
        if speed == SPEED_OFF:
            await self.async_turn_off()
        else:
            if self._state != True:
                await super().async_turn_on()
            if speed:
                await self.async_set_speed(speed)

    @property
    def speed(self):
        spd = int(self._state_attrs.get('gears',0))
        if spd >= 2:
            return SPEED_HIGH
        elif spd >= 1:
            return SPEED_MEDIUM
        return SPEED_LOW

    @property
    def speed_list(self):
        return [SPEED_LOW, SPEED_MEDIUM, SPEED_HIGH]

    @staticmethod
    def speed_to_gears(speed = None):
        spd = 0
        if speed == SPEED_MEDIUM:
            spd = 1
        if speed == SPEED_HIGH:
            spd = 2
        return spd


    async def async_set_speed(self, speed: Optional[str] = None):
        spd = self.speed_to_gears(speed)
        _LOGGER.debug('Setting speed for %s: %s(%s)', self._name, speed, spd)
        result = await self.async_set_property('gears', spd)
        if result:
            if 'gears' in self._state_attrs:
                self._state_attrs.update({
                    'gears': spd,
                })

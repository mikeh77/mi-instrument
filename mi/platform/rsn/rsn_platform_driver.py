#!/usr/bin/env python

"""
@package ion.agents.platform.rsn.rsn_platform_driver
@file    ion/agents/platform/rsn/rsn_platform_driver.py
@author  Carlos Rueda
@brief   The main RSN OMS platform driver class.
"""
import time

from mi.core.exceptions import InstrumentException


__author__ = 'Carlos Rueda'
__license__ = 'Apache 2.0'

from copy import deepcopy
import mi.core.log

log = mi.core.log.get_logger()
from functools import partial
from mi.core.common import BaseEnum
from mi.core.scheduler import PolledScheduler
from mi.platform.platform_driver import PlatformDriver
from mi.core.instrument.data_particle import DataParticle, DataParticleKey
from mi.core.instrument.instrument_driver import DriverAsyncEvent
from mi.platform.platform_driver import PlatformDriverState
from mi.platform.platform_driver import PlatformDriverEvent
from mi.platform.exceptions import PlatformException
from mi.platform.exceptions import PlatformDriverException
from mi.platform.exceptions import PlatformConnectionException
from mi.platform.rsn.oms_client_factory import CIOMSClientFactory
from mi.platform.responses import InvalidResponse


# from pyon.util.containers import get_ion_ts

import ntplib


# from pyon.event.event import EventSubscriber

# from pyon.agent.common import BaseEnum
# from pyon.agent.instrument_fsm import InstrumentException
# 
# from pyon.core.object import ion_serializer, IonObjectDeserializer
# from pyon.core.registry import IonObjectRegistry
# from ion.core.ooiref import OOIReferenceDesignator

from mi.platform.util.node_configuration import NodeConfiguration


class PlatformParticle(DataParticle):
    """
    The contents of the parameter dictionary, published at the start of a scan
    """

    def _build_parsed_values(self):
        return [{DataParticleKey.VALUE_ID: a, DataParticleKey.VALUE: b} for a, b in self.raw_data]


class ScheduledJob(BaseEnum):
    """
    Instrument scheduled jobs
    """
    ACQUIRE_SAMPLE = 'pad_sample_timer_event'


class RSNPlatformDriverState(PlatformDriverState):
    """
    We simply inherit the states from the superclass
    """
    pass


class RSNPlatformDriverEvent(PlatformDriverEvent):
    """
    The ones for superclass plus a few others for the CONNECTED state.
    """
    GET_ENG_DATA              = 'RSN_PLATFORM_DRIVER_GET_ENG_DATA'
    TURN_ON_PORT              = 'RSN_PLATFORM_DRIVER_TURN_ON_PORT'
    TURN_OFF_PORT             = 'RSN_PLATFORM_DRIVER_TURN_OFF_PORT'
    SET_PORT_OVER_CURRENT_LIMITS             = 'RSN_PLATFORM_DRIVER_SET_PORT_OVER_CURRENT_LIMITS'
    START_PROFILER_MISSION    = 'RSN_PLATFORM_DRIVER_START_PROFILER_MISSION'
    STOP_PROFILER_MISSION     = 'RSN_PLATFORM_DRIVER_STOP_PROFILER_MISSION'
    GET_MISSION_STATUS        = 'RSN_PLATFORM_DRIVER_GET_MISSION_STATUS'
    GET_AVAILABLE_MISSIONS    = 'RSN_PLATFORM_DRIVER_GET_AVAILABLE_MISSIONS'

class RSNPlatformDriverCapability(BaseEnum):
    GET_ENG_DATA              = RSNPlatformDriverEvent.GET_ENG_DATA
    TURN_ON_PORT              = RSNPlatformDriverEvent.TURN_ON_PORT
    TURN_OFF_PORT             = RSNPlatformDriverEvent.TURN_OFF_PORT
    SET_PORT_OVER_CURRENT_LIMITS             = RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS
    START_PROFILER_MISSION    = RSNPlatformDriverEvent.START_PROFILER_MISSION
    STOP_PROFILER_MISSION     = RSNPlatformDriverEvent.STOP_PROFILER_MISSION
    GET_MISSION_STATUS         = RSNPlatformDriverEvent.GET_MISSION_STATUS
    GET_AVAILABLE_MISSIONS     = RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS


class RSNPlatformDriver(PlatformDriver):
    """
    The main RSN OMS platform driver class.
    """

    def __init__(self, event_callback):
        """
        Creates an RSNPlatformDriver instance.
        @param event_callback  Listener of events generated by this driver
        """
        PlatformDriver.__init__(self, event_callback)

        # CIOMSClient instance created by connect() and destroyed by disconnect():
        self._rsn_oms = None

        # URL for the event listener registration/unregistration (based on
        # web server launched by ServiceGatewayService, since that's the
        # service in charge of receiving/relaying the OMS events).
        # NOTE: (as proposed long ago), this kind of functionality should
        # actually be provided by some component more in charge of the RSN
        # platform netwokr as a whole -- as opposed to platform-specific).
        self.listener_url = None
        
        # scheduler config is a bit redundant now, but if we ever want to
        # re-initialize a scheduler we will need it.
        self._scheduler = None
        
        self._lastRcvSampleTime = 0

    def _filter_capabilities(self, events):
        """
        """
        events_out = [x for x in events if RSNPlatformDriverCapability.has(x)]
        return events_out

    def validate_driver_configuration(self, driver_config):
        """
        Driver config must include 'oms_uri' entry.
        """
        if not 'oms_uri' in driver_config:
            log.error("'oms_uri' not present in driver_config = %s", driver_config)
            raise PlatformDriverException(msg="driver_config does not indicate 'oms_uri'")

    def _configure(self, driver_config):
        """
        Nothing special done here, only calls super.configure(driver_config)

        @param driver_config with required 'oms_uri' entry.
        """
        PlatformDriver._configure(self, driver_config)

        self.nodeCfg = NodeConfiguration()

        self._platform_id = driver_config['node_id']
        self.nodeCfg.openNode(self._platform_id, driver_config['driver_config_file']['node_cfg_file'])

        if 'nms_source' in self.nodeCfg.node_meta_data :
            self.nms_source = self.nodeCfg.node_meta_data['nms_source']
        else:
            self.nms_source = 0
            
        if 'oms_sample_rate' in self.nodeCfg.node_meta_data :
            self.oms_sample_rate = self.nodeCfg.node_meta_data['oms_sample_rate']
        else:
            self.oms_sample_rate = 60

        self.nodeCfg.Print()

        self._construct_resource_schema()

    def _build_scheduler(self):
        """
        Build a scheduler for periodic status updates
        """
        self._scheduler = PolledScheduler()
        self._scheduler.start()

        def event_callback(event):
            log.info("driver job triggered, raise event: %s" % event)
            self._fsm.on_event(event)

        # Dynamically create the method and add it
        method = partial(event_callback, RSNPlatformDriverEvent.GET_ENG_DATA)

        self._job = self._scheduler.add_interval_job(method, seconds=self.oms_sample_rate)

    def _delete_scheduler(self):
        """
        Remove the autosample schedule.
        """
        try:
            self._scheduler.unschedule_job(self._job)
            self._scheduler.shutdown()
 #           self._scheduler.remove_scheduler(self._job)
        except KeyError:
            log.info('Failed to remove scheduled job for ACQUIRE_SAMPLE')

    def _construct_resource_schema(self):
        """
        """
        parameters = deepcopy(self._param_dict)

        for k, v in parameters.iteritems():
            read_write = v.get('read_write', None)
            if read_write == 'write':
                v['visibility'] = 'READ_WRITE'
            else:
                v['visibility'] = 'READ_ONLY'

        commands = {}
        commands[RSNPlatformDriverEvent.TURN_ON_PORT] = \
            {
                "display_name": "Port Power On",
                "description": "Activate port power.",
                "args": [],
                "kwargs": {
                    'port_id': {
                        "required": True,
                        "type": "string",
                    }
                }

            }
        commands[RSNPlatformDriverEvent.TURN_OFF_PORT] = \
            {
                "display_name": "Port Power Off",
                "description": "Deactivate port power.",
                "args": [],
                "kwargs": {
                    'port_id': {
                        "required": True,
                        "type": "string",
                    }
                }
            }

        self._resource_schema['parameters'] = parameters
        self._resource_schema['commands'] = commands

    def _ping(self):
        """
        Verifies communication with external platform returning "PONG" if
        this verification completes OK.

        @retval "PONG" iff all OK.
        @raise PlatformConnectionException Cannot ping external platform or
               got unexpected response.
        """
        log.debug("%r: pinging OMS...", self._platform_id)
        self._verify_rsn_oms('_ping')

        try:
            retval = self._rsn_oms.hello.ping()
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot ping: %s" % str(e))

        if retval is None or retval.upper() != "PONG":
            raise PlatformConnectionException(msg="Unexpected ping response: %r" % retval)

        log.debug("%r: ping completed: response: %s", self._platform_id, retval)

        return "PONG"

    def callback_for_alert(self, event, *args, **kwargs):
        log.debug("caught an OMSDeviceStatusEvent: %s", event)

        #        self._notify_driver_event(OMSEventDriverEvent(event['description']))

        log.info('Platform agent %r published OMSDeviceStatusEvent : %s, time: %s',
                 self._platform_id, event, time.time())


    def _connect(self, recursion=None):
        """
        Creates an CIOMSClient instance, does a ping to verify connection,
        and starts event dispatch.
        """
        # create CIOMSClient:
        oms_uri = self._driver_config['oms_uri']
        log.debug("%r: creating CIOMSClient instance with oms_uri=%r",
                  self._platform_id, oms_uri)
        self._rsn_oms = CIOMSClientFactory.create_instance(oms_uri)
        log.debug("%r: CIOMSClient instance created: %s",
                  self._platform_id, self._rsn_oms)

        # ping to verify connection:
        self._ping()

 
        self.get_eng_data() # call this once right away
 
        self._build_scheduler() # then start calling it every X seconds

 


    def _disconnect(self, recursion=None):



        CIOMSClientFactory.destroy_instance(self._rsn_oms)
        self._rsn_oms = None
        log.debug("%r: CIOMSClient instance destroyed", self._platform_id)

        self._delete_scheduler()
        self._scheduler = None

    def get_metadata(self):
        """
        """

        return self.nodeCfg.meta_data

  
        
        
    def get_eng_data(self):
        

        log.debug("%r: get_eng_data...", self._platform_id)

        ntp_time = ntplib.system_to_ntp_time(time.time())
        
        if self._lastRcvSampleTime==0:   # first time this is called set this to a reasonable value
            self._lastRcvSampleTime = ntp_time - self.oms_sample_rate*2

        if self._lastRcvSampleTime<ntp_time-self.oms_sample_rate*10 :    #prevent the max lookback time getting to big  
            self._lastRcvSampleTime=ntp_time-self.oms_sample_rate*10     #if we stop getting data for some reason
        

        for streamKey, stream in sorted(self.nodeCfg.node_streams.iteritems()):
            log.debug("%r Stream(%s)", self._platform_id, streamKey)
            attrs = list()
            for streamAttrKey, streamAttr in sorted(stream.iteritems()):
                #               log.debug("%r     %r = %r", self._platform_id, streamAttrKey,streamAttr)
                    attrs.append((streamAttrKey,self._lastRcvSampleTime+0.1)) # add a little bit of time to the last received so we don't get one we already have again
            
            if len(attrs)>0 :
                log.error("%r Request From OMS Stream(%s) Attrs(%s)", self._platform_id, streamKey,attrs)
            
                returnDict = self.get_attribute_values_from_oms(attrs) #go get the data from the OMS
                                
                ts_list = self.get_all_returned_timestamps(returnDict) #get the list of all unique returned timestamps
        
                log.error("%r Request From OMS Stream(%s) Return (%s)", self._platform_id, streamKey,returnDict)
 
                
                for ts in sorted(ts_list): #for each timestamp create a particle and emit it
                    oneTimestampAttrs = self.get_single_timestamp_list(stream,ts,returnDict) #go get the list at this timestamp
                    ionOneTimestampAttrs = self.convertAttrsToIon(stream,oneTimestampAttrs) #scale the attrs and convert the names to ion
              
                    pad_particle = PlatformParticle(ionOneTimestampAttrs,
                                                    preferred_timestamp=DataParticleKey.INTERNAL_TIMESTAMP) #need to review what port timetamp meaning is..
              
                    pad_particle.set_internal_timestamp(timestamp=ts)
              
                    pad_particle._data_particle_type = streamKey  # stream name
              
                    json_message = pad_particle.generate() # this cals parse values above to go from raw to values dict
       
                    event = {
                         'type': DriverAsyncEvent.SAMPLE,
                         'value': json_message,
                         'time': time.time()
                    }
            
                    self._send_event(event)
                    self._lastRcvSampleTime=ts
#                   log.error("---------%r Last Recv Time Stamp Return (%f)", self._lastRcvSampleTime)

        return 1


   


    def get_attribute_values(self, attrs):
        """Simple wrapper method for compatibility.
        """
        return self.get_attribute_values_from_oms(attrs)


    def get_attribute_values_from_oms(self,attrs):
        """
        """
        if not isinstance(attrs, (list, tuple)):
            raise PlatformException('get_attribute_values: attrs argument must be a '
                                    'list [(attrName, from_time), ...]. Given: %s', attrs)

        self._verify_rsn_oms('get_attribute_values_from_oms')
        
        log.debug("get_attribute_values: attrs=%s", self._platform_id)
        log.debug("get_attribute_values: attrs=%s", attrs)

        try:
            response = self._rsn_oms.attr.get_platform_attribute_values(self._platform_id,
                                                                        attrs)
        except Exception as e:
            raise PlatformConnectionException(msg="get_attribute_values_from_oms Cannot get_platform_attribute_values: %s" % str(e))

        dic_plat = self._verify_platform_id_in_response(response)

        # reported timestamps are already in NTP. Just return the dict:
        return dic_plat

    def get_all_returned_timestamps(self, attrs):

        ts_list = list()

        #go through all of the returned values and get the unique timestamps. Each
        #particle will have data for a unique timestamp
        for attr_id, attr_vals in attrs.iteritems():
            if not( isinstance(attr_vals,list)):
                log.debug("Invalid attr_vals %s attrs=%s",attr_id, attr_vals) #in case we get an INVALID_ATTRIBUTE_ID
            else:
                for v, ts in attr_vals:
                    if not ts in ts_list:
                        ts_list.append(ts)

        return(ts_list)
    
    
    def get_single_timestamp_list(self,stream,ts_in, attrs):

        #create a list of sample data from just the single timestamp

        newAttrList = []  #key value list for this timestamp

        for key in stream:  # assuming we will put all values in stream even if we didn't get a sample this time
            found_ts_match = 0
            if key in attrs:
                for v, ts in attrs[key]:
                    if(ts==ts_in):
                        if(found_ts_match==0):
                            newAttrList.append((key,v))
                            found_ts_match=1


        return(newAttrList)
    
 

    def convertAttrsToIon(self, stream, attrs):
        """
        """

        attrs_return = []

        #convert back to ION parameter name and scale from OMS to ION            
        for key, v in attrs:
            scaleFactor = stream[key]['scale_factor']
            if v is None:
                attrs_return.append((stream[key]['ion_parameter_name'], v))
            else:
                attrs_return.append((stream[key]['ion_parameter_name'], v * scaleFactor))

                #       log.debug("Back to ION=%s", attrs_return)

        return attrs_return


    def _verify_platform_id_in_response(self, response):
        """
        Verifies the presence of my platform_id in the response.

        @param response Dictionary returned by _rsn_oms

        @retval response[self._platform_id]
        """
        if not self._platform_id in response:
            msg = "unexpected: response does not contain entry for %r" % self._platform_id
            log.error(msg)
            raise PlatformException(msg=msg)

        if response[self._platform_id] == InvalidResponse.PLATFORM_ID:
            msg = "response reports invalid platform_id for %r" % self._platform_id
            log.error(msg)
            raise PlatformException(msg=msg)
        else:
            return response[self._platform_id]


    def set_overcurrent_limit(self, port_id, milliamps, microseconds, src):
        self._verify_rsn_oms('set_overcurrent_limit')
        oms_port_cntl_id = self._verify_and_return_oms_port(port_id, 'set_overcurrent_limit')

        try:
            response = self._rsn_oms.port.set_over_current(self._platform_id, oms_port_cntl_id, int(milliamps),
                                                           int(microseconds), src)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot set_overcurrent_limit: %s" % str(e))

        response = self._convert_port_id_from_oms_to_ci(port_id, oms_port_cntl_id, response)
        log.debug("set_overcurrent_limit = %s", response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform


    def turn_on_port(self, port_id, src):
        self._verify_rsn_oms('turn_on_port')
        oms_port_cntl_id = self._verify_and_return_oms_port(port_id, 'turn_on_port')

        log.debug("%r: turning on port: port_id=%s oms port_id = %s",
                  self._platform_id, port_id, oms_port_cntl_id)

        try:
            response = self._rsn_oms.port.turn_on_platform_port(self._platform_id,
                                                                oms_port_cntl_id, src)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot turn_on_platform_port: %s" % str(e))

        response = self._convert_port_id_from_oms_to_ci(port_id, oms_port_cntl_id, response)
        log.debug("%r: turn_on_platform_port response: %s",
                  self._platform_id, response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform

    def turn_off_port(self, port_id, src):
        self._verify_rsn_oms('turn_off_port')
        oms_port_cntl_id = self._verify_and_return_oms_port(port_id, 'turn_off_port')

        log.debug("%r: turning off port: port_id=%s oms port_id = %s",
                  self._platform_id, port_id, oms_port_cntl_id)

        try:
            response = self._rsn_oms.port.turn_off_platform_port(self._platform_id,
                                                                 oms_port_cntl_id, src)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot turn_off_platform_port: %s" % str(e))

        response = self._convert_port_id_from_oms_to_ci(port_id, oms_port_cntl_id, response)
        log.debug("%r: turn_off_platform_port response: %s",
                  self._platform_id, response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform

    def start_profiler_mission(self, mission_name, src):
        self._verify_rsn_oms('start_profiler_mission')

        try:
            response = self._rsn_oms.profiler.start_mission(self._platform_id,
                                                                mission_name,src)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot start_profiler_mission: %s" % str(e))

        log.debug("%r: start_profiler_mission response: %s",
                  self._platform_id, response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform

    def stop_profiler_mission(self,flag,src):
        self._verify_rsn_oms('stop_profiler_mission')

        try:
            response = self._rsn_oms.profiler.stop_mission(self._platform_id,flag,src)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot stop_profiler_mission: %s" % str(e))

        log.debug("%r: stop_profiler_mission response: %s",
                  self._platform_id, response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform

    def get_mission_status(self):
        self._verify_rsn_oms('get_mission_status')

        try:
            response = self._rsn_oms.profiler.get_mission_status(self._platform_id)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot get_mission_status: %s" % str(e))

        log.debug("%r: get_mission_status response: %s",
                  self._platform_id, response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform
    
    def get_available_missions(self):
        self._verify_rsn_oms('get_available_missions')

        try:
            response = self._rsn_oms.profiler.get_available_missions(self._platform_id)
        except Exception as e:
            raise PlatformConnectionException(msg="Cannot get_available_missions: %s" % str(e))

        log.debug("%r: get_available_missions response: %s",
                  self._platform_id, response)

        dic_plat = self._verify_platform_id_in_response(response)

        return dic_plat  # note: return the dic for the platform


    def _verify_rsn_oms(self, method_name):
        if self._rsn_oms is None:
            raise PlatformConnectionException(
                "Cannot %s: _rsn_oms object required (created via connect() call)" % method_name)

    def _verify_and_return_oms_port(self, port_id, method_name):
        if port_id not in self.nodeCfg.node_port_info:
            raise PlatformConnectionException("Cannot %s: Invalid Port ID" % method_name)

        return self.nodeCfg.node_port_info[port_id]['port_oms_port_cntl_id']
        
    def _convert_port_id_from_oms_to_ci(self, port_id, oms_port_cntl_id, response):
        """
        Converts the OMS port id into the original one provided.
        """
        if response[self._platform_id].get(oms_port_cntl_id, None):
            return {self._platform_id: {port_id: response[self._platform_id].get(oms_port_cntl_id, None)}}

        return response


    ###############################################
    # External event handling:

    def _register_event_listener(self, url):
        """
        Registers given url for all event types.
        """
        self._verify_rsn_oms('_register_event_listener')

        try:
            already_registered = self._rsn_oms.event.get_registered_event_listeners()
        except Exception as e:
            raise PlatformConnectionException(
                msg="%r: Cannot get registered event listeners: %s" % (self._platform_id, e))

        if url in already_registered:
            log.debug("listener %r was already registered", url)
            return

        try:
            result = self._rsn_oms.event.register_event_listener(url)
        except Exception as e:
            raise PlatformConnectionException(
                msg="%r: Cannot register_event_listener: %s" % (self._platform_id, e))

        log.debug("%r: register_event_listener(%r) => %s", self._platform_id, url, result)

    def _unregister_event_listener(self, url):
        """
        Unregisters given url for all event types.
        """
        self._verify_rsn_oms('_unregister_event_listener')

        try:
            result = self._rsn_oms.event.unregister_event_listener(url)
        except Exception as e:
            raise PlatformConnectionException(
                msg="%r: Cannot unregister_event_listener: %s" % (self._platform_id, e))

        log.debug("%r: unregister_event_listener(%r) => %s", self._platform_id, url, result)

   

    ##############################################################
    # GET
    ##############################################################

    def get(self, *args, **kwargs):

        if 'attrs' in kwargs:
            attrs = kwargs['attrs']
            result = self.get_attribute_values(attrs)
            return result

        if 'metadata' in kwargs:
            result = self.get_metadata()
            return result

        return super(RSNPlatformDriver, self).get(*args, **kwargs)


    ##############################################################
    # EXECUTE
    ##############################################################

    def execute(self, cmd, *args, **kwargs):
        """
        Executes the given command.

        @param cmd   command

        @return  result of the execution
        """

        if cmd == RSNPlatformDriverEvent.TURN_ON_PORT:
            result = self.turn_on_port(*args, **kwargs)

        elif cmd == RSNPlatformDriverEvent.TURN_OFF_PORT:
            result = self.turn_off_port(*args, **kwargs)

        elif cmd == RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS:
            result = self.set_port_over_current_limits(*args, **kwargs)

        elif cmd == RSNPlatformDriverEvent.START_PROFILER_MISSION:
            result = self.start_profiler_mission(*args, **kwargs)

        elif cmd == RSNPlatformDriverEvent.STOP_PROFILER_MISSION:
            result = self.stop_profiler_mission(*args, **kwargs)

        elif cmd == RSNPlatformDriverEvent.GET_MISSION_STATUS:
            result = self.get_mission_status(*args, **kwargs)

        elif cmd == RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS:
            result = self.get_available_missions(*args, **kwargs)

        else:
            result = super(RSNPlatformDriver, self).execute(cmd, args, kwargs)

        return result

    def _get_ports(self):
        log.debug("%r: _get_ports: %s", self._platform_id, self.nodeCfg.node_port_list)
        return self.nodeCfg.node_port_list


    def _handler_connected_start_profiler_mission(self, *args, **kwargs):
        """
        """
#        profile_mission_name = kwargs.get('profile_mission_name', None)

        profile_mission_name = kwargs.get('profile_mission_name', 'Test_Profile_Mission_Name')
        if profile_mission_name is None:
            raise InstrumentException('start_profiler_mission: missing profile_mission_name argument')

        src = kwargs.get('src', None)
        if src is None:
            raise InstrumentException('set_port_over_current_limits: missing src argument')

        try:
            result = self.start_profiler_mission(profile_mission_name,src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.START_PROFILER_MISSION,
                                         args, kwargs, e)
            
    def _handler_connected_stop_profiler_mission(self, *args, **kwargs):

        """
        """
        
        flag = kwargs.get('flag', None)
        if milliamps is None:
            raise InstrumentException('_handler_connected_stop_profiler_mission: missing flag argument')

        src = kwargs.get('src', None)
        if src is None:
            raise InstrumentException('set_port_over_current_limits: missing src argument')

        try:
            result = self.stop_profiler_mission()
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.STOP_PROFILER_MISSION,
                                         args, kwargs, e)   
            
    def _handler_connected_get_mission_status(self, *args, **kwargs):

        """
        """
        try:
            result = self.get_mission_status()
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.GET_MISSION_STATUS,
                                         args, kwargs, e)  
             
    def _handler_connected_get_available_missions(self, *args, **kwargs):
        """
        """
        try:
            result = self.get_available_missions()
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS,
                                         args, kwargs, e)   

            
            
    def _handler_connected_get_eng_data(self, *args, **kwargs):
        """
        """

        try:
            result = self.get_eng_data()
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.GET_ENG_DATA,
                                         args, kwargs, e)

    def _handler_connected_set_port_over_current_limits(self, *args, **kwargs):
        """
        """
        port_id = kwargs.get('port_id', None)
        if port_id is None:
            raise InstrumentException('set_port_over_current_limits: missing port_id argument')

        milliamps = kwargs.get('milliamps', None)
        if milliamps is None:
            raise InstrumentException('set_port_over_current_limits: missing milliamps argument')

        microseconds = kwargs.get('microseconds', None)
        if milliamps is None:
            raise InstrumentException('set_port_over_current_limits: missing microseconds argument')

        src = kwargs.get('src', None)
        if src is None:
            raise InstrumentException('set_port_over_current_limits: missing src argument')

        try:
            result = self.set_port_over_current_limits(port_id, milliamps, microseconds, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS,
                                         args, kwargs, e)


    def _handler_connected_turn_on_port(self, *args, **kwargs):
        """
        """
        port_id = kwargs.get('port_id', None)
        if port_id is None:
            raise InstrumentException('turn_on_port: missing port_id argument')

        src = kwargs.get('src', None)
        if port_id is None:
            raise InstrumentException('turn_on_port: missing src argument')

        try:
            result = self.turn_on_port(port_id, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.TURN_ON_PORT,
                                         args, kwargs, e)

    def _handler_connected_turn_off_port(self, *args, **kwargs):
        """
        """
        port_id = kwargs.get('port_id', None)
        if port_id is None:
            raise InstrumentException('turn_off_port: missing port_id argument')

        src = kwargs.get('src', None)
        if port_id is None:
            raise InstrumentException('turn_off_port: missing src argument')

        try:
            result = self.turn_off_port(port_id)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.TURN_OFF_PORT,
                                         args, kwargs, e)


    ##############################################################
    # RSN Platform driver FSM setup
    ##############################################################

    def _construct_fsm(self,
                       states=RSNPlatformDriverState,
                       events=RSNPlatformDriverEvent,
                       enter_event=RSNPlatformDriverEvent.ENTER,
                       exit_event=RSNPlatformDriverEvent.EXIT):
        """
        """
        super(RSNPlatformDriver, self)._construct_fsm(states, events,
                                                      enter_event, exit_event)

        # CONNECTED state event handlers we add in this class:
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.TURN_ON_PORT, self._handler_connected_turn_on_port)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS, self._handler_connected_set_port_over_current_limits)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.TURN_OFF_PORT, self._handler_connected_turn_off_port)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.START_PROFILER_MISSION, self._handler_connected_start_profiler_mission)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.STOP_PROFILER_MISSION, self._handler_connected_stop_profiler_mission)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.GET_MISSION_STATUS, self._handler_connected_get_mission_status)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS, self._handler_connected_get_available_missions)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.GET_ENG_DATA, self._handler_connected_get_eng_data)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, ScheduledJob.ACQUIRE_SAMPLE, self._handler_connected_get_eng_data)


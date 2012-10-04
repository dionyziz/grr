#!/usr/bin/env python

# Copyright 2011 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This file contains a helper class for the flows.

This flow context class provides all the methods for handling flows (i.e.,
calling clients, changing state, ...).
"""


import os
import pdb
import struct
import threading
import time
import traceback


from grr.client import conf as flags
import logging
from grr.client import actions
from grr.lib import aff4
from grr.lib import data_store
from grr.lib import scheduler
from grr.lib import stats
from grr.lib import utils
from grr.proto import jobs_pb2


# Session ids below this range are reserved.
RESERVED_RANGE = 100
DEFAULT_WORKER_QUEUE_NAME = "W"


class FlowContextError(Exception):
  """Raised when there is an error during state transitions."""


class MoreDataException(Exception):
  """Raised when there is more data available."""


class HuntFlowContext(object):
  """The flow context class for hunts.

  This is essentially the same as a normal context but it processes
  all the requests that arrive regardless of any order such that one client that
  doesn't respond does not make the whole hunt wait.
  """

  process_requests_in_order = False

  def __init__(self, client_id=None, flow_name=None,
               queue_name=DEFAULT_WORKER_QUEUE_NAME,
               event_id=None, state=None, args=None, priority=None, _store=None,
               token=None):
    """Constructor for the FlowContext.

    Args:
      client_id: The name of the client we are working with.
      flow_name: The name of the flow class of this context.
      queue_name: The name of the queue that the messages will run
                  with (default is W for general purpose workers).

      event_id: A logging event id for issuing further logs.
      state: A protobuf containing state information for this context.
      args: A protodict containing the arguments that the flow was run with.
      priority: The priority of this flow.
      _store: An optional data store to use. (Usually only used in tests).
      token: An instance of data_store.ACLToken security token.
    """
    self.queue_name = queue_name
    self.session_id = self._GetNewSessionID(queue_name)
    self.client_id = client_id

    if token is None:
      token = data_store.ACLToken()

    self.token = token

    # Any new requests go here during flow execution, and then we Flush them to
    # the task scheduler at once.
    self.new_request_states = []

    # These indicate the next states that are allowed from here
    self.next_states = []
    self.current_state = "Start"

    self.next_processed_request = 1
    self.next_outbound_id = 1

    self._outstanding_requests = 0
    self.data_store = _store or data_store.DB

    self.user = token.username

    self.outbound_lock = threading.Lock()

    if event_id is None:
      # If flow didn't come from the frontend or a parent flow it
      # probably came from the console so we generate an ID.
      event_id = "%s:console" % self.user

    if flow_name is None:
      flow_name = "unknown"

    # We create a flow_pb for us to be stored in
    self.flow_pb = jobs_pb2.FlowPB(session_id=self.session_id,
                                   create_time=long(time.time() * 1e6),
                                   state=jobs_pb2.FlowPB.RUNNING,
                                   name=flow_name,
                                   creator=token.username,
                                   event_id=event_id)

    if priority is not None:
      self.flow_pb.priority = priority

    if client_id is not None:
      self.flow_pb.client_id = client_id

    if state is not None:
      self.flow_pb.request_state.MergeFrom(state)

    if args is not None:
      self.flow_pb.args.MergeFrom(args)

  def SetFlowObj(self, parent_flow):
    self.parent_flow = parent_flow

  def SetState(self, state):
    self.flow_pb.state = state

  def SetStatus(self, status):
    self.flow_pb.status = status

  def GetFlowArgs(self):
    """Shortcut function to get the arguments passed to the flow."""
    return utils.ProtoDict(self.flow_pb.args).ToDict()

  def GetNextOutboundId(self):
    with self.outbound_lock:
      my_id = self.next_outbound_id
      self.next_outbound_id += 1
    return my_id

  def CallState(self, messages=None, next_state="", client_id=None):
    """This method is used to schedule a new state on a different worker.

    This is basically the same as CallFlow() except we are calling
    ourselves. The state will be invoked in a later time and receive all the
    messages we send.

    Args:
       messages: A list of protobufs to send. If the last one is not a
            GrrStatus, we append an OK Status.
       next_state: The state in this flow to be invoked with the responses.
       client_id: This client_id is used to schedule the request.

    Raises:
       FlowContextError: if the next state is not valid.
    """
    if messages is None:
      messages = []

    # Check if the state is valid
    if not getattr(self.parent_flow, next_state):
      raise FlowContextError("Next state %s is invalid.")

    # Queue the response message to the parent flow
    with FlowManager(self.session_id, token=self.token,
                     store=self.data_store) as flow_manager:
      outbound_id = self.GetNextOutboundId()
      # Create a new request state
      request_state = jobs_pb2.RequestState(id=outbound_id,
                                            session_id=self.session_id,
                                            client_id=client_id,
                                            next_state=next_state)

      flow_manager.QueueRequest(request_state)

      # Add the status message if needed.
      if not messages or not isinstance(messages[-1], jobs_pb2.GrrStatus):
        messages.append(jobs_pb2.GrrStatus())

      # Send all the messages
      for i, payload in enumerate(messages):
        msg = jobs_pb2.GrrMessage(session_id=self.session_id,
                                  request_id=request_state.id, response_id=1+i,
                                  auth_state=jobs_pb2.GrrMessage.AUTHENTICATED,
                                  args=payload.SerializeToString())

        if isinstance(payload, jobs_pb2.GrrStatus):
          msg.type = jobs_pb2.GrrMessage.STATUS

        flow_manager.QueueResponse(msg)

      self._outstanding_requests += 1

      # Also schedule the last status message on the worker queue.
      scheduler.SCHEDULER.Schedule(
          [scheduler.SCHEDULER.Task(queue=self.queue_name, value=msg)],
          token=self.token)

      # And notify the worker about it.
      scheduler.SCHEDULER.NotifyQueue(self.queue_name, self.session_id,
                                      self.token)

  def _GetNewSessionID(self, queue_name):
    """Returns a random integer session ID.

    This id is used to refer to the serialized flow in the task
    master. If a collision occurs the flow objects will be
    overwritten.

    TODO(user): Check the task scheduler here for a session of this
    same ID to avoid possible collisions.

    Args:
      queue_name: The name of the queue to prefix to the session id

    Returns:
      a formatted session id string
    """
    while 1:
      result = struct.unpack("l", os.urandom(struct.calcsize("l")))[0] % 2**32
      # Ensure session ids are larger than the reserved ones
      if result > RESERVED_RANGE:
        return "%s:%X" % (queue_name, result)

  def ProcessCompletedRequests(self, thread_pool):
    """Go through the list of requests and process the completed ones.

    We take a snapshot in time of all requests and responses for this flow. We
    then process as many completed requests as possible. If responses are not
    quite here we leave it for next time.

    It is safe to call this function as many times as needed. NOTE: We assume
    that the flow queue is locked so another worker is not processing these
    messages while we are. It is safe to insert new messages to the flow:state
    queue.

    Args:
      thread_pool: For regular flows, the messages have to be processed in
                   order. Thus, the thread_pool argument is only used for hunts.
    """
    processing = []
    try:
      # The flow is dead - remove all outstanding requests and responses.
      if not self.IsRunning():
        self.parent_flow.Log("Flow dead - deleting all outstanding requests.")
        with FlowManager(self.session_id, token=self.token,
                         store=self.data_store) as flow_manager:
          for request, responses in flow_manager.FetchRequestsAndResponses():
            flow_manager.DeleteFlowRequestStates(request, responses)

        return

      with FlowManager(self.session_id, token=self.token,
                       store=self.data_store) as flow_manager:
        for request, responses in flow_manager.FetchRequestsAndResponses():
          if request.id == 0:
            continue

          # Are there any responses at all?
          if not responses:
            continue

          if self.process_requests_in_order:

            if request.id > self.next_processed_request:
              break

            # Not the request we are looking for
            if request.id < self.next_processed_request:
              flow_manager.DeleteFlowRequestStates(request, responses)
              continue

            if request.id != self.next_processed_request:
              stats.STATS.Increment("grr_response_out_of_order")
              break

          # Check if the responses are complete (Last response must be a STATUS
          # message).
          if responses[-1].type != jobs_pb2.GrrMessage.STATUS:
            continue

          # At this point we process this request - we can remove all requests
          # and responses from the queue.
          flow_manager.DeleteFlowRequestStates(request, responses)

          # Do we have all the responses here?
          if len(responses) != responses[-1].response_id:
            # If we can retransmit do so. Note, this is different from the
            # automatic retransmission facilitated by the task scheduler (the
            # Task.ttl field) which would happen regardless of these.
            if request.transmission_count < 5:
              request.transmission_count += 1
              self.new_request_states.append(request)
            break

          # If we get here its all good - run the flow.
          if self.IsRunning():
            self._Process(request, responses, thread_pool, processing)
          # Quit early if we are no longer alive.
          else: break

          self.next_processed_request += 1
          self._outstanding_requests -= 1

        # Are there any more outstanding requests?
        if not self.parent_flow.OutstandingRequests():
          # Allow the flow to cleanup
          if self.IsRunning():
            try:
              if self.current_state != "End":
                self.parent_flow.End()
            except Exception:
              # This flow will terminate now
              stats.STATS.Increment("grr_flow_errors")
              self.Error(self.client_id, traceback.format_exc())

        # This allows the End state to issue further client requests - hence
        # postpone termination.
        if not self.parent_flow.OutstandingRequests():
          stats.STATS.Increment("grr_flow_completed_count")
          logging.info("Destroying session %s(%s) for client %s",
                       self.session_id, self.__class__.__name__, self.client_id)

          self.Terminate()

    except MoreDataException:
      # We did not read all the requests/responses in this run in order to
      # keep a low memory footprint and have to make another pass.
      scheduler.SCHEDULER.NotifyQueue(self.queue_name, self.session_id,
                                      self.token)

    finally:
      # We wait here until all threads are done processing and we can safely
      # pickle the flow object.
      for event in processing:
        event.wait()

  def _Process(self, request, responses, thread_pool, events=None):
    event = threading.Event()
    events.append(event)
    # In a hunt, all requests are independent and can be processed
    # in separate threads.
    thread_pool.AddTask(target=self._ProcessSingleRequest,
                        args=(request, responses, event,),
                        name="Hunt processing")

  def _ProcessSingleRequest(self, request, responses, event=None):
    """Completes the request by calling the state method.

    NOTE - we expect the state method to be suitably decorated with a
     StateHandler (otherwise this will raise because the prototypes
     are different)

    Args:
      request: A RequestState protobuf.
      responses: A list of GrrMessages responding to the request.
      event: A threading.Event() instance to signal completion of this request.
    """

    try:
      self.current_state = request.next_state
      client_id = request.client_id or self.client_id
      logging.info("%s Running %s with %d responses from %s",
                   self.session_id, request.next_state,
                   len(responses), client_id)
      getattr(self.parent_flow, request.next_state)(request=request,
                                                    responses=responses)
    # We don't know here what exceptions can be thrown in the flow but we have
    # to continue. Thus, we catch everything.
    except Exception:
      # This flow will terminate now
      stats.STATS.Increment("grr_flow_errors")

      self.Error(client_id, traceback.format_exc())

    finally:
      if event:
        event.set()

  def CallClient(self, action_name, args_pb=None, next_state=None,
                 request_data=None, client_id=None, **kwargs):
    """Calls the client asynchronously.

    This sends a message to the client to invoke an Action. The run
    action may send back many responses. These will be queued by the
    framework until a status message is sent by the client. The status
    message will cause the entire transaction to be committed to the
    specified state.

    Args:
       action_name: The function to call on the client.

       args_pb: A protobuf to send to the client. If not specified (Or None) we
             create a new protobuf using the kwargs.

       next_state: The state in this flow, that responses to this
       message should go to.

       request_data: A dict which will be available in the RequestState
             protobuf. The Responses object maintains a reference to this
             protobuf for use in the execution of the state method. (so you can
             access this data by responses.request.data). Valid values are
             strings, unicode and protobufs.
       client_id: The request is sent to this client.

    Raises:
       FlowContextError: If next_state is not one of the allowed next states.
    """
    if args_pb is None:
      # Retrieve the correct protobuf to use to send to the action
      try:
        proto_cls = actions.ActionPlugin.classes[action_name].in_protobuf
      except KeyError:
        proto_cls = None

      if proto_cls is None: proto_cls = jobs_pb2.DataBlob

      args_pb = proto_cls(**kwargs)

    # Check that the next state is allowed
    if next_state is None:
      raise FlowContextError("next_state is not specified for CallClient")

    if self.process_requests_in_order and next_state not in self.next_states:
      raise FlowContextError("Flow %s: State '%s' called to '%s' which is "
                             "not declared in decorator." % (
                                 self.__class__.__name__,
                                 self.current_state,
                                 next_state))

    outbound_id = self.GetNextOutboundId()
    # Create a new request state
    state = jobs_pb2.RequestState(id=outbound_id,
                                  session_id=self.session_id,
                                  next_state=next_state,
                                  client_id=client_id)

    if request_data is not None:
      state.data.MergeFrom(utils.ProtoDict(request_data).ToProto())

    # Send the message with the request state
    msg = jobs_pb2.GrrMessage(
        session_id=self.session_id, name=action_name,
        request_id=outbound_id, args=args_pb.SerializeToString(),
        priority=self.flow_pb.priority)
    state.request.MergeFrom(msg)

    # Remember the new request for later
    self.new_request_states.append(state)

    self._outstanding_requests += 1

  def CallFlow(self, flow_factory, flow_name, next_state=None,
               request_data=None, client_id=None, **kwargs):
    """Creates a new flow and send its responses to a state.

    This creates a new flow. The flow may send back many responses which will be
    queued by the framework until the flow terminates. The final status message
    will cause the entire transaction to be committed to the specified state.

    Args:
       flow_factory: A FlowFactory object.
       flow_name: The name of the flow to invoke.

       next_state: The state in this flow, that responses to this
       message should go to.

       request_data: Any string provided here will be available in the
             RequestState protobuf. The Responses object maintains a reference
             to this protobuf for use in the execution of the state method. (so
             you can access this data by responses.request.data). There is no
             format mandated on this data but it may be a serialized protobuf.

       client_id: If given, the flow is started for this client.

       **kwargs: Arguments for the child flow.

    Raises:
       FlowContextError: If next_state is not one of the allowed next states.
    """
    if self.process_requests_in_order:
      # Check that the next state is allowed
      if next_state and next_state not in self.next_states:
        raise FlowContextError("Flow %s: State '%s' called to '%s' which is "
                               "not declared in decorator." % (
                                   self.__class__.__name__,
                                   self.current_state,
                                   next_state))

    client_id = client_id or self.client_id
    outbound_id = self.GetNextOutboundId()

    # This looks very much like CallClient() above - we prepare a request state,
    # and add it to our queue - any responses from the child flow will return to
    # the request state and the stated next_state. Note however, that there is
    # no client_id or actual request message here because we directly invoke the
    # child flow rather than queue anything for it.
    state = jobs_pb2.RequestState(id=outbound_id,
                                  session_id=self.session_id,
                                  client_id=client_id,
                                  next_state=next_state, flow_name=flow_name,
                                  response_count=0)

    if request_data:
      state.data.MergeFrom(utils.ProtoDict(request_data).ToProto())

    # Create the new child flow but do not notify the user about it.
    child = flow_factory.StartFlow(
        client_id, flow_name,
        event_id=self.flow_pb.event_id,
        _request_state=state, token=self.token,
        notify_to_user=False, _store=self.data_store,
        _parent_request_queue=self.new_request_states,
        queue_name=self.queue_name, **kwargs)

    # Add the request state to the queue.
    self.new_request_states.append(state)

    # Keep track of our children.
    self.flow_pb.children.append(child)

    self._outstanding_requests += 1

  def SendReply(self, response_proto):
    """Allows this flow to send a message to its parent flow.

    If this flow does not have a parent, the message is ignored.

    Args:
      response_proto: A protobuf to be sent to the parent.
    """
    # We have a parent only if we know our parent's request state.
    if self.flow_pb.HasField("request_state"):
      request_state = self.flow_pb.request_state

      request_state.response_count += 1
      worker_queue = request_state.session_id.split(":")[0]
      try:
        # queue the response message to the parent flow
        with FlowManager(request_state.session_id, token=self.token,
                         store=self.data_store) as flow_manager:

          if isinstance(response_proto, jobs_pb2.GrrStatus):
            # Also send resource usage information to the parent flow.
            user_cpu = self.flow_pb.cpu_used.user_cpu_time
            sys_cpu = self.flow_pb.cpu_used.system_cpu_time
            response_proto.cpu_time_used.user_cpu_time = user_cpu
            response_proto.cpu_time_used.system_cpu_time = sys_cpu
            response_proto.network_bytes_sent = self.flow_pb.network_bytes_sent
            response_proto.child_session_id = self.session_id

          # Make a response message
          msg = jobs_pb2.GrrMessage(
              session_id=request_state.session_id,
              request_id=request_state.id,
              response_id=request_state.response_count,
              auth_state=jobs_pb2.GrrMessage.AUTHENTICATED,
              args=response_proto.SerializeToString())

          if isinstance(response_proto, jobs_pb2.GrrStatus):
            msg.type = jobs_pb2.GrrMessage.STATUS

            # Status messages are also sent to their worker queues
            scheduler.SCHEDULER.Schedule(
                [scheduler.SCHEDULER.Task(queue=worker_queue, value=msg)],
                token=self.token)

          # Queue the response now
          flow_manager.QueueResponse(msg)
      except MoreDataException:
        pass

      finally:
        scheduler.SCHEDULER.NotifyQueue(worker_queue, request_state.session_id,
                                        self.token)

  def FlushMessages(self):
    """Flushes the messages that were queued with CallClient."""
    with self.outbound_lock:
      to_flush = self.new_request_states
      self.new_request_states = []

    # The most important thing here is to adjust request.ts_id to the correct
    # task scheduler id which we get when queuing the messages in the requests.
    # We schedule all the tasks at once on the client queue, then adjust the
    # ts_id and then queue the request states on the flow's state queue.
    for destination, requests in utils.GroupBy(to_flush,
                                               lambda x: x.client_id):

      to_schedule = [request for request in requests if request.request.name]

      # The requests contain messages - schedule the messages on the client's
      # queue
      tasks = [scheduler.SCHEDULER.Task(queue=destination,
                                        value=request.request)
               for request in to_schedule]

      # This will update task.id to the correct value
      scheduler.SCHEDULER.Schedule(tasks, token=self.token, sync=True)

      stats.STATS.Add("grr_worker_requests_issued", len(tasks))

      # Now adjust the request state to point to the task id
      for request, task in zip(to_schedule, tasks):
        request.ts_id = task.id

    # Now store all RequestState proto in their flow state
    for session_id, requests in utils.GroupBy(to_flush,
                                              lambda x: x.session_id):
      try:
        with FlowManager(session_id, token=self.token,
                         store=self.data_store) as flow_manager:
          for request in requests:
            flow_manager.QueueRequest(request)
      except MoreDataException:
        pass

  def Error(self, client_id, backtrace=None):
    """Logs an error for a client."""
    self.parent_flow.LogClientError(client_id, backtrace=backtrace)

  def IsRunning(self):
    return self.flow_pb.state == jobs_pb2.FlowPB.RUNNING

  def Terminate(self):
    """Terminates this flow."""
    try:
      # Dequeue existing requests
      with FlowManager(self.session_id, token=self.token,
                       store=self.data_store) as flow_manager:
        flow_manager.DestroyFlowStates()
    except MoreDataException:
      pass

    # Just mark as terminated
    # This flow might already not be running
    if self.flow_pb.state == jobs_pb2.FlowPB.RUNNING:
      logging.debug("Terminating flow %s", self.session_id)
      self.SendReply(jobs_pb2.GrrStatus())
      self.flow_pb.state = jobs_pb2.FlowPB.TERMINATED
      self.parent_flow.Save()

  def OutstandingRequests(self):
    """Returns the number of all outstanding requests.

    This is used to determine if the flow needs to be destroyed yet.

    Returns:
       the number of all outstanding requests.
    """
    return self._outstanding_requests

  def __getstate__(self):
    """Controls pickling of this object."""
    stats.STATS.Increment("grr_worker_flows_pickled")

    # We have to copy the dict here because we need to pickle the flow first
    # and flush the requests later to avoid a race. Thus, we have to keep a
    # copy of the new_request_states around for flushing.
    to_pickle = self.__dict__.copy()
    to_pickle["new_request_states"] = []
    to_pickle["data_store"] = None
    to_pickle["flow_pb"] = None
    to_pickle["outbound_lock"] = None

    return to_pickle

  def SaveResourceUsage(self, request, responses):
    status = responses.status
    user_cpu = status.cpu_time_used.user_cpu_time
    system_cpu = status.cpu_time_used.system_cpu_time
    self.flow_pb.cpu_used.user_cpu_time += user_cpu
    self.flow_pb.cpu_used.system_cpu_time += system_cpu
    self.flow_pb.network_bytes_sent += status.network_bytes_sent

    if status.child_session_id:
      fd = self.parent_flow.GetAFF4Object(mode="w", age=aff4.NEWEST_TIME,
                                          token=self.token)
      resources = fd.Schema.RESOURCES()
      resources.data.client_id = request.client_id
      resources.data.session_id = status.child_session_id
      resources.data.cpu_usage.user_cpu_time = user_cpu
      resources.data.cpu_usage.system_cpu_time = system_cpu
      resources.data.network_bytes_sent = status.network_bytes_sent

      fd.AddAttribute(resources)
      fd.Close(sync=False)


class FlowContext(HuntFlowContext):
  """The flow context class."""

  process_requests_in_order = True

  def _Process(self, request, responses, unused_thread_pool=None, event=None):
    self._ProcessSingleRequest(request, responses, event=event)

  def CallState(self, messages=None, next_state="", client_id=None):
    client_id = client_id or self.client_id
    HuntFlowContext.CallState(self, messages=messages, next_state=next_state,
                              client_id=client_id)

  def CallClient(self, action_name, args_pb=None, next_state=None,
                 request_data=None, client_id=None, **kwargs):
    client_id = client_id or self.client_id
    HuntFlowContext.CallClient(self, action_name, args_pb=args_pb,
                               next_state=next_state, request_data=request_data,
                               client_id=client_id, **kwargs)

  def CallFlow(self, flow_factory, flow_name, next_state=None,
               request_data=None, client_id=None, **kwargs):
    client_id = client_id or self.client_id
    HuntFlowContext.CallFlow(self, flow_factory, flow_name,
                             next_state=next_state, request_data=request_data,
                             client_id=client_id, **kwargs)

  def Error(self, client_id, backtrace=None):
    """Kills this flow with an error."""
    if self.flow_pb.state == jobs_pb2.FlowPB.RUNNING:
      self.flow_pb.state = jobs_pb2.FlowPB.ERROR
      # Set an error status
      reply = jobs_pb2.GrrStatus(status=jobs_pb2.GrrStatus.GENERIC_ERROR)
      if backtrace:
        reply.error_message = backtrace
      self.SendReply(reply)

      if backtrace:
        logging.error("Error in flow %s (%s). Trace: %s", self.session_id,
                      client_id, backtrace)
        self.flow_pb.backtrace = backtrace
      else:
        logging.error("Error in flow %s (%s).", self.session_id, client_id)

      self.parent_flow.Save()
      if flags.FLAGS.debug:
        pdb.set_trace()

      self.parent_flow.Notify(
          "FlowStatus", self.client_id,
          "Flow (%s) terminated due to error" % self.session_id)

  def SaveResourceUsage(self, _, responses):
    status = responses.status
    user_cpu = status.cpu_time_used.user_cpu_time
    system_cpu = status.cpu_time_used.system_cpu_time
    self.flow_pb.cpu_used.user_cpu_time += user_cpu
    self.flow_pb.cpu_used.system_cpu_time += system_cpu
    self.flow_pb.network_bytes_sent += status.network_bytes_sent


class FlowManager(object):
  """This class manages the representation of the flow within the data store."""
  # These attributes are related to a flow's internal data structures
  # Requests are protobufs of type RequestState. They have a column
  # prefix followed by the request number:
  FLOW_REQUEST_PREFIX = "flow:request:"
  FLOW_REQUEST_TEMPLATE = FLOW_REQUEST_PREFIX + "%08X"

  # This regex will return all messages (requests or responses) in this flow
  # state.
  FLOW_MESSAGE_REGEX = "flow:.*"

  # This regex will return all the requests in order
  FLOW_REQUEST_REGEX = FLOW_REQUEST_PREFIX + ".*"

  # Each request may have any number of responses. These attributes
  # are GrrMessage protobufs. Their attribute consist of a prefix,
  # followed by the request number, followed by the response number.
  FLOW_RESPONSE_PREFIX = "flow:response:%08X:"
  FLOW_RESPONSE_TEMPLATE = FLOW_RESPONSE_PREFIX + "%08X"

  # This regex will return all the responses in order
  FLOW_RESPONSE_REGEX = "flow:response:.*"

  # This is the subject name of flow state variables. We need to be
  # able to lock these independently from the actual flow.
  FLOW_TASK_TEMPLATE = "task:%s"
  FLOW_STATE_TEMPLATE = "task:%s:state"

  FLOW_TASK_REGEX = "task:.*"

  request_limit = 10000
  response_limit = 100000

  def __init__(self, session_id, store=None, sync=True, token=None):
    self.session_id = session_id
    self.subject = self.FLOW_STATE_TEMPLATE % session_id
    self.sync = sync
    self.token = token
    if store is None:
      store = data_store.DB

    self.data_store = store

    # We cache all these and write/delete in one operation.
    self.to_write = {}
    self.to_delete = []
    self.client_messages = []
    self.client_id = None

  def FetchRequestsAndResponses(self):
    """Fetches all outstanding requests and responses for this flow.

    We first cache all requests and responses for this flow in memory to
    prevent round trips.

    Yields:
      an tuple (request protobufs, list of responses messages) in ascending
      order of request ids.

    Raises:
      MoreDataException: When there is more data available than read by the
                         limited query.
    """
    subject = self.FLOW_STATE_TEMPLATE % self.session_id
    state_map = {0: {"REQUEST_STATE": jobs_pb2.RequestState(id=0)}}
    max_request_id = "00000000"

    request_count = 0
    response_count = 0
    # Get some requests
    for predicate, serialized, _ in sorted(self.data_store.ResolveRegex(
        subject, self.FLOW_REQUEST_REGEX, token=self.token,
        limit=self.request_limit)):
      components = predicate.split(":")
      max_request_id = components[2]

      request = jobs_pb2.RequestState()
      request.ParseFromString(serialized)

      meta_data = state_map.setdefault(request.id, {})
      meta_data["REQUEST_STATE"] = request
      request_count += 1

    # Now get some responses
    for predicate, serialized, _ in sorted(self.data_store.ResolveRegex(
        subject, self.FLOW_RESPONSE_REGEX, token=self.token,
        limit=self.response_limit)):
      response_count += 1
      components = predicate.split(":")
      if components[2] > max_request_id:
        break

      response = jobs_pb2.GrrMessage()
      response.ParseFromString(serialized)

      if response.request_id in state_map:
        meta_data = state_map.setdefault(response.request_id, {})
        responses = meta_data.setdefault("RESPONSES", [])
        responses.append(response)

    for request_id in sorted(state_map):
      try:
        metadata = state_map[request_id]
        yield (metadata["REQUEST_STATE"], metadata.get("RESPONSES", []))
      except KeyError:
        pass

    if (request_count >= self.request_limit or
        response_count >= self.response_limit):
      raise MoreDataException()

  def DeleteFlowRequestStates(self, request_state, responses):
    """Deletes the request and all its responses from the flow state queue."""

    if request_state:
      self.to_delete.append(self.FLOW_REQUEST_TEMPLATE % request_state.id)

      # Remove the message from the client queue that this request forms.
      self.client_messages.append(request_state.ts_id)
      self.client_id = request_state.client_id

    # Delete all the responses by their response id.
    for response in responses:
      self.to_delete.append(self.FLOW_RESPONSE_TEMPLATE % (
          response.request_id, response.response_id))

  def DestroyFlowStates(self):
    """Deletes all states in this flow and dequeue all client messages."""
    for request_state, _ in self.FetchRequestsAndResponses():
      if request_state:
        self.client_messages.append(request_state.ts_id)
        self.client_id = request_state.client_id

    self.data_store.DeleteSubject(self.subject, token=self.token)

  def Flush(self):
    """Writes the changes in this object to the datastore."""
    try:
      self.data_store.MultiSet(self.subject, self.to_write, sync=self.sync,
                               to_delete=self.to_delete, token=self.token)

    except data_store.Error:
      pass

    scheduler.SCHEDULER.Delete(self.client_id, self.client_messages,
                               token=self.token)

    self.to_write = {}
    self.to_delete = []

  def __enter__(self):
    return self

  def __exit__(self, unused_type, unused_value, unused_traceback):
    """Supports 'with' protocol."""
    self.Flush()

  def QueueResponse(self, message_proto):
    """Queues the message on this flow's state."""
    # Insert to the data_store
    self.to_write.setdefault(
        FlowManager.FLOW_RESPONSE_TEMPLATE % (
            message_proto.request_id, message_proto.response_id),
        []).append(message_proto.SerializeToString())

  def QueueRequest(self, request_state):
    self.to_write.setdefault(
        self.FLOW_REQUEST_TEMPLATE % request_state.id, []).append(
            request_state.SerializeToString())


class WellKnownFlowManager(FlowManager):
  """A flow manager for well known flows."""

  def FetchRequestsAndResponses(self):
    """Well known flows do not have real requests.

    This manages retrieving all the responses without requiring corresponding
    requests.

    Yields:
      A tuple of request (None) and responses.
    """
    subject = self.FLOW_STATE_TEMPLATE % self.session_id

    # Get some requests
    for _, serialized, _ in sorted(self.data_store.ResolveRegex(
        subject, self.FLOW_RESPONSE_REGEX, token=self.token,
        limit=self.request_limit)):

      # The predicate format is flow:response:REQUEST_ID:RESPONSE_ID. For well
      # known flows both request_id and response_id are randomized.
      response = jobs_pb2.GrrMessage()
      response.ParseFromString(serialized)

      yield None, [response]

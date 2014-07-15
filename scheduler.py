#!/usr/bin/env python

from __future__ import print_function

import logging
import os
import sys
import time
import json
from pprint import pprint
import uuid

import mesos
import mesos_pb2
import requests


class HTTPProxyScheduler(mesos.Scheduler):
  def __init__(self, executor, service):
    self.executor = executor
    self.service = service
    # self.taskData = {}
    self.tasksLaunched = 0
    self.tasksFinished = 0

  def registered(self, driver, frameworkId, masterInfo):
    """
      Invoked when the scheduler successfully registers with a Mesos master.
      It is called with the frameworkId, a unique ID generated by the
      master, and the masterInfo which is information about the master
      itself.
    """
    logging.warn("Registered with framework ID %s" % frameworkId.value)

  def reregistered(self, driver, masterInfo):
    """
      Invoked when the scheduler re-registers with a newly elected Mesos
      master.  This is only called when the scheduler has previously been
      registered.  masterInfo contains information about the newly elected
      master.
    """
    logging.warn("Re-Registered with new master")

  def disconnected(self, driver):
    """
      Invoked when the scheduler becomes disconnected from the master, e.g.
      the master fails and another is taking over.
    """
    logging.warn("Disconnected from master")

  @staticmethod
  def _decode_typed_field(pb):
    field_type = pb.type
    if field_type == 0: # Scalar
      return pb.scalar.value
    elif field_type == 1: # Ranges
      return [{"begin": ra.begin, "end": ra.end} for ra in pb.ranges.range]
    elif field_type == 2: # Set
      return pb.set.item
    elif field_type == 3: # Text
      return pb.text
    else:
      raise Exception("Unknown field type: %s" % field_type)

  @staticmethod
  def _decode_attribute(attr_pb):
    return {attr_pb.name: HTTPProxyScheduler._decode_typed_field(attr_pb)}

  @staticmethod
  def _decode_resource(resource_pb):
    return (resource_pb.name, HTTPProxyScheduler._decode_typed_field(resource_pb))

  def resourceOffers(self, driver, offers):
    """
      Invoked when resources have been offered to this framework. A single
      offer will only contain resources from a single slave.  Resources
      associated with an offer will not be re-offered to _this_ framework
      until either (a) this framework has rejected those resources (see
      SchedulerDriver.launchTasks) or (b) those resources have been
      rescinded (see Scheduler.offerRescinded).  Note that resources may be
      concurrently offered to more than one framework at a time (depending
      on the allocator being used).  In that case, the first framework to
      launch tasks using those resources will be able to use them while the
      other frameworks will have those resources rescinded (or if a
      framework has already launched tasks with those resources then those
      tasks will fail with a TASK_LOST status and a message saying as much).
    """
    logging.info("Got %d resource offers" % len(offers))

    for offer in offers:
      # protobuf -> dict
      info = {
        "attributes": [HTTPProxyScheduler._decode_attribute(a) for a in offer.attributes],
        "executor_ids": [ei.value for ei in offer.executor_ids],
        "framework_id": offer.framework_id.value,
        "hostname": offer.hostname,
        "id": offer.id.value,
        "resources": {name: value for (name, value) in [HTTPProxyScheduler._decode_resource(r) for r in offer.resources]},
        "slave_id": offer.slave_id.value,
      }

      logging.debug("Offer: " + json.dumps(info, sort_keys=True, indent=2, separators=(',', ': ')))

      resp = requests.post(self.service + "offer",
                           data=json.dumps(info),
                           headers={'content-type': 'application/json'})
      tasks_to_run = resp.json()

      tasks = []
      for task_to_run in tasks_to_run:
        tid = task_to_run["id"]
        self.tasksLaunched += 1

        logging.info("Accepting offer on %s to start task %s" % (offer.hostname, tid))

        task = mesos_pb2.TaskInfo()
        task.task_id.value = str(tid)
        task.slave_id.value = offer.slave_id.value
        task.name = "task %s" % tid
        task.executor.MergeFrom(self.executor)

        cpus = task.resources.add()
        cpus.name = "cpus"
        cpus.type = mesos_pb2.Value.SCALAR
        cpus.scalar.value = task_to_run["resources"]["cpus"]

        mem = task.resources.add()
        mem.name = "mem"
        mem.type = mesos_pb2.Value.SCALAR
        mem.scalar.value = task_to_run["resources"]["mem"]

        tasks.append(task)
        # self.taskData[task.task_id.value] = (offer.slave_id, task.executor.executor_id)
      driver.launchTasks(offer.id, tasks)

  def offerRescinded(self, driver, offerId):
    """
      Invoked when an offer is no longer valid (e.g., the slave was lost or
      another framework used resources in the offer.) If for whatever reason
      an offer is never rescinded (e.g., dropped message, failing over
      framework, etc.), a framwork that attempts to launch tasks using an
      invalid offer will receive TASK_LOST status updats for those tasks
      (see Scheduler.resourceOffers).
    """
    logging.info("Offer rescinded: %s" % offerId.value)

  def statusUpdate(self, driver, update):
    """
      Invoked when the status of a task has changed (e.g., a slave is lost
      and so the task is lost, a task finishes and an executor sends a
      status update saying so, etc.) Note that returning from this callback
      acknowledges receipt of this status update.  If for whatever reason
      the scheduler aborts during this callback (or the process exits)
      another status update will be delivered.  Note, however, that this is
      currently not true if the slave sending the status update is lost or
      fails during that time.
    """

    # TODO: handle each of these
    # TASK_STAGING = 6;  // Initial state. Framework status updates should not use.
    # TASK_STARTING = 0;
    # TASK_RUNNING = 1;
    # TASK_FINISHED = 2; // TERMINAL.
    # TASK_FAILED = 3;   // TERMINAL.
    # TASK_KILLED = 4;   // TERMINAL.
    # TASK_LOST = 5;     // TERMINAL.

    logging.info("Task %s is in state %d" % (update.task_id.value, update.state))

    if update.state == mesos_pb2.TASK_FINISHED:
      self.tasksFinished += 1

      # slave_id, executor_id = self.taskData[update.task_id.value]

      # driver.sendFrameworkMessage(
      #   executor_id,
      #   slave_id,
      #   "Task %s finished" % update.task_id.value)

  def frameworkMessage(self, driver, executorId, slaveId, message):
    """
      Invoked when an executor sends a message. These messages are best
      effort; do not expect a framework message to be retransmitted in any
      reliable fashion.
    """
    logging.info("Received message: %s" % repr(str(message)))

  def slaveLost(self, driver, slaveId):
    """
      Invoked when a slave has been determined unreachable (e.g., machine
      failure, network partition.) Most frameworks will need to reschedule
      any tasks launched on this slave on a new slave.
    """
    logging.warn("Slave lost: %s" % slaveId.value)

  def executorLost(self, driver, executorId, slaveId, status):
    """
      Invoked when an executor has exited/terminated. Note that any tasks
      running will have TASK_LOST status updates automatically generated.
    """
    logging.warn("Executor %s lost on slave %s" % (exeuctorId.value, slaveId.value))

  def error(self, driver, message):
    """
      Invoked when there is an unrecoverable error in the scheduler or
      scheduler driver.  The driver will be aborted BEFORE invoking this
      callback.
    """
    logging.error("Error from Mesos: %s" % message)


if __name__ == "__main__":
  if len(sys.argv) != 2:
    print("Usage: %s master_host:master_port" % sys.argv[0])
    sys.exit(1)

  # TODO: take these on cmdline
  log_level = "DEBUG"
  service = "http://localhost:5000/"

  logging.basicConfig(level=getattr(logging, log_level.upper()))

  executor = mesos_pb2.ExecutorInfo()
  executor.executor_id.value = "default"
  executor.command.value = os.path.abspath("./executor.py")
  executor.name = "HTTP Proxy Executor"
  executor.source = "http_proxy"

  framework = mesos_pb2.FrameworkInfo()
  framework.user = "" # Have Mesos fill in the current user.
  framework.name = "HTTP Proxy Framework"
  framework.principal = "http-proxy"

  driver = mesos.MesosSchedulerDriver(
    HTTPProxyScheduler(executor, service),
    framework,
    sys.argv[1])

  status = 0 if driver.run() == mesos_pb2.DRIVER_STOPPED else 1

  # Ensure that the driver process terminates.
  driver.stop();

  sys.exit(status)
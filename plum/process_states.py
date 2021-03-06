from enum import Enum
import logging
import traceback
from plum.wait import WaitOn, Interrupted
import plum.util as util
from plum.process_listener import ProcessListener


class ProcessState(Enum):
    """
    The possible states that a :class:`Process` can be in.
    """
    CREATED = 0
    RUNNING = 1
    WAITING = 2
    STOPPED = 3
    FAILED = 4


class State(object):
    @classmethod
    def create_from(cls, process, saved_state):
        """
        Create the process state from a saved instance state

        :param process: The process this state belongs to
        :param saved_state: The saved instance state
        :return: The wait on with its state as it was when it was saved
        :rtype: This class type
        """
        state = cls.__new__(cls)
        state.load_instance_state(process, saved_state)
        return state

    def __init__(self, process):
        """
        :param process: The process
        :type process: :class:`plum.process.Process`
        :param: The process state lock
        :type: :class:`threading.Lock`
        """
        self._process = process

    @property
    def label(self):
        return self.LABEL

    def enter(self, previous_state):
        self._process.log_with_pid(logging.DEBUG, "entering state '{}'".format(self.label))

    def execute(self):
        self._process.log_with_pid(logging.DEBUG, "executing state '{}'".format(self.label))
        return None

    def exit(self):
        self._process.log_with_pid(logging.DEBUG, "exiting state '{}'".format(self.label))

    def interrupt(self):
        pass

    def save_instance_state(self, out_state):
        out_state['class_name'] = util.fullname(self)

    def load_instance_state(self, process, saved_state):
        self._process = process


class Created(State):
    LABEL = ProcessState.CREATED

    def __init__(self, process):
        super(Created, self).__init__(process)

    def enter(self, previous_state):
        super(Created, self).enter(previous_state)
        self._process._on_create()

    def execute(self):
        super(Created, self).execute()
        return Running(self._process, self._process.do_run)


class Running(State):
    LABEL = ProcessState.RUNNING
    EXEC_FUNC = 'exec_func'

    @staticmethod
    def _is_wait_retval(retval):
        """
        Determine if the value provided is a valid Wait return value which consists
        of a 2-tuple of a WaitOn and a callback function (or None) to be called
        after the wait on is ready

        :param retval: The return value from a step to check
        :return: True if it is a valid wait object, False otherwise
        """
        return (isinstance(retval, tuple) and
                len(retval) == 2 and
                isinstance(retval[0], WaitOn))

    def __init__(self, process, exec_func, *args, **kwargs):
        """
        :param process: The process this state belongs to
        :type process: :class:`plum.process.Process`
        :param exec_func: The run function to call during this state  
        """
        super(Running, self).__init__(process)
        self._exec_func = exec_func
        self._args = args
        self._kwargs = kwargs

    def enter(self, previous_state):
        super(Running, self).enter(previous_state)

        if previous_state is ProcessState.CREATED:
            self._process._on_start()
            self._process._fire_listener_event(ProcessListener.on_process_start)
        elif previous_state is ProcessState.WAITING:
            self._process._on_resume()
            self._process._fire_listener_event(ProcessListener.on_process_resume)
        elif previous_state is ProcessState.RUNNING:
            pass
        else:
            raise RuntimeError("Cannot enter RUNNING from '{}'".format(previous_state))

        self._process._on_run()
        self._process._fire_listener_event(ProcessListener.on_process_run)

    def execute(self):
        super(Running, self).execute()

        retval = self._exec_func(*self._args, **self._kwargs)
        if Running._is_wait_retval(retval):
            wait_on, callback = retval
            # # Check if we can continue straight away
            # if wait_on.wait(timeout=0):
            #     if callback is None:
            #         return Stopped(self._process)
            #     else:
            #         return Running(self._process, callback, wait_on)
            # else:
            #     return Waiting(self._process, wait_on, callback)
            return Waiting(self._process, wait_on, callback)
        else:
            return Stopped(self._process)

    def save_instance_state(self, out_state):
        super(Running, self).save_instance_state(out_state)
        out_state[self.EXEC_FUNC] = self._exec_func.__name__

    def load_instance_state(self, process, saved_state):
        super(Running, self).load_instance_state(process, saved_state)
        self._exec_func = getattr(self._process, saved_state[self.EXEC_FUNC])


class Waiting(State):
    LABEL = ProcessState.WAITING
    WAIT_ON = 'wait_on'
    CALLBACK = 'callback'

    def __init__(self, process, wait_on, callback):
        """
        :param process: The process this state belongs to
        :type process: :class:`plum.process.Process`
        :param wait_on: The WaitOn to wait for
        :type wait_on: :class:`plum.wait.WaitOn`
        :param callback: A callback function for the next Running state after
            finished waiting.  Can be None in which case next state will be
            STOPPED.
        """
        super(Waiting, self).__init__(process)
        self._wait_on = wait_on
        self._callback = callback

    def enter(self, previous_state):
        super(Waiting, self).enter(previous_state)
        self._process._on_wait(self._wait_on)
        self._process._fire_listener_event(ProcessListener.on_process_wait)

    def execute(self):
        super(Waiting, self).execute()

        self._wait_on.wait()
        if self._callback is None:
            return Stopped(self._process)
        else:
            return Running(self._process, self._callback, self._wait_on)

    def interrupt(self):
        self._wait_on.interrupt()

    def save_instance_state(self, out_state):
        super(Waiting, self).save_instance_state(out_state)
        out_state[self.WAIT_ON] = \
            self._process.save_wait_on_state(self._wait_on, self._callback)
        out_state[self.CALLBACK] = self._callback.__name__

    def load_instance_state(self, process, saved_state):
        super(Waiting, self).load_instance_state(process, saved_state)

        try:
            self._callback = getattr(self._process, saved_state[self.CALLBACK])
        except AttributeError:
            raise ValueError(
                "This process does not have a function with "
                "the name '{}' as expected from the wait on".
                    format(saved_state[self.CALLBACK]))

        self._wait_on = self._process.create_wait_on(saved_state[self.WAIT_ON], self._callback)

    def get_wait_on(self):
        return self._wait_on


class Stopped(State):
    LABEL = ProcessState.STOPPED

    def __init__(self, process, abort=False, abort_msg=None):
        super(Stopped, self).__init__(process)
        self._abort = abort
        self._abort_msg = abort_msg

    def enter(self, previous_state):
        super(Stopped, self).enter(previous_state)

        if self._abort:
            self._process._on_abort(self._abort_msg)
            self._process._fire_listener_event(ProcessListener.on_process_abort)
        elif previous_state is ProcessState.RUNNING:
            self._process._on_finish()
            self._process._fire_listener_event(ProcessListener.on_process_finish)
        else:
            raise RuntimeError("Cannot enter STOPPED from '{}'".format(previous_state))

        self._process._on_stop(self._abort_msg)
        self._process._fire_listener_event(ProcessListener.on_process_stop)

    def save_instance_state(self, out_state):
        super(Stopped, self).save_instance_state(out_state)
        out_state['abort'] = self._abort
        out_state['abort_msg'] = self._abort_msg

    def load_instance_state(self, process, saved_state):
        super(Stopped, self).load_instance_state(process, saved_state)
        self._abort = saved_state['abort']
        self._abort_msg = saved_state['abort_msg']

    def get_abort_msg(self):
        return self._abort_msg

    def get_aborted(self):
        return self._abort


class Failed(State):
    LABEL = ProcessState.FAILED

    def __init__(self, process, exc_info):
        super(Failed, self).__init__(process)
        self._exc_info = exc_info

    def enter(self, previous_state):
        super(Failed, self).enter(previous_state)
        try:
            self._process._on_fail(self._exc_info)
        except BaseException:
            import traceback
            self._process.log_with_pid(
                logging.ERROR, "exception entering failed state:\n{}".format(traceback.format_exc()))

        self._process._fire_listener_event(ProcessListener.on_process_fail)

    def save_instance_state(self, out_state):
        super(Failed, self).save_instance_state(out_state)

        exc_info = self._exc_info
        # Saving traceback can be problematic so don't bother, just store None
        out_state['exc_info'] = (exc_info[0], exc_info[1], None)

    def load_instance_state(self, process, saved_state):
        super(Failed, self).load_instance_state(process, saved_state)
        super(Failed, self).load_instance_state(saved_state)
        self._exc_info = saved_state['exc_info']

    def get_exc_info(self):
        return self._exc_info


def load_state(process, state_bundle):
    # Get the class using the class loader and instantiate it
    class_name = state_bundle['class_name']
    proc_class = state_bundle.get_class_loader().load_class(class_name)
    return proc_class.create_from(process, state_bundle)

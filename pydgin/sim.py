#=======================================================================
# sim.py
#=======================================================================
# This is the common top-level simulator. ISA implementations can use
# various hooks to configure the behavior.

import os
import sys
import pickle
import csv

# ensure we know where the pypy source code is
# XXX: removed the dependency to PYDGIN_PYPY_SRC_DIR because rpython
# libraries are much slower than native python when running on an
# interpreter. So unless the user have added rpython source to their
# PYTHONPATH, we should use native python.
#try:
#  sys.path.append( os.environ['PYDGIN_PYPY_SRC_DIR'] )
#except KeyError as e:
#  print "NOTE: PYDGIN_PYPY_SRC_DIR not defined, using pure python " \
#        "implementation"

from pydgin.debug import Debug, pad, pad_hex
from pydgin.misc  import FatalError
from pydgin.jit   import JitDriver, hint, set_user_param, set_param, \
                         elidable

def jitpolicy(driver):
  from rpython.jit.codewriter.policy import JitPolicy
  return JitPolicy()

#-------------------------------------------------------------------------
# Sim
#-------------------------------------------------------------------------
# Abstract simulator class

class Sim( object ):

  def __init__( self, arch_name, jit_enabled=False ):

    self.arch_name   = arch_name

    self.jit_enabled = jit_enabled

    if jit_enabled:
      self.jitdriver = JitDriver( greens =['pc', 'core_id'],
                                  reds   = ['tick_ctr', 'max_insts', 'state', 'sim',],
                                  #virtualizables  =['state',],
                                  get_printable_location=self.get_location,
                                )

      # Set the default trace limit here. Different ISAs can override this
      # value if necessary
      self.default_trace_limit = 400000

    self.max_insts = 0

    self.ncores = 1
    self.core_switch_ival = 1
    self.pkernel_bin = None

  #-----------------------------------------------------------------------
  # decode
  #-----------------------------------------------------------------------
  # This needs to be implemented in the child class

  def decode( self, bits ):
    raise NotImplementedError()

  #-----------------------------------------------------------------------
  # init_state
  #-----------------------------------------------------------------------
  # This needs to be implemented in the child class

  def init_state( self, exe_file, exe_name, run_argv, testbin ):
    raise NotImplementedError()

  #-----------------------------------------------------------------------
  # help message
  #-----------------------------------------------------------------------
  # the help message to display on --help

  help_message = """
  Pydgin %s Instruction Set Simulator
  usage: %s <args> <sim_exe> <sim_args>

  <sim_exe>  the executable to be simulated
  <sim_args> arguments to be passed to the simulated executable
  <args>     the following optional arguments are supported:

    --help,-h       Show this message and exit
    --test          Run in testing mode (for running asm tests)
    --env,-e <NAME>=<VALUE>
                    Set an environment variable to be passed to the
                    simulated program. Can use multiple --env flags to set
                    multiple environment variables.
    --debug,-d <flags>[:<start_after>]
                    Enable debug flags in a comma-separated form (e.g.
                    "--debug syscalls,insts"). If provided, debugs starts
                    after <start_after> cycles. The following flags are
                    supported:
         insts              cycle-by-cycle instructions
         rf                 register file accesses
         mem                memory accesses
         regdump            register dump
         syscalls           syscall information
         bootstrap          initial stack and register state

    --max-insts <i> Run until the maximum number of instructions
    --ncores <i>    Number of cores to simulate
    --core-switch-ival <i>
                    Switch cores at this interval
    --pkernel <f>   Load pkernel binary
    --jit <flags>   Set flags to tune the JIT (see
                    rpython.rlib.jit.PARAMETER_DOCS)
  """

  #-----------------------------------------------------------------------
  # get_location
  #-----------------------------------------------------------------------
  # for debug printing in PYPYLOG

  @staticmethod
  def get_location( pc, core_id ):
    # TODO: add the disassembly of the instruction here as well
    return "pc: %x core_id: %x" % ( pc, core_id )

  @elidable
  def next_core_id( self, core_id ):
    return ( core_id + 1 ) % self.ncores

  #-----------------------------------------------------------------------
  # run
  #-----------------------------------------------------------------------
  def run( self ):
    self = hint( self, promote=True )

    #s = self.state

    max_insts = self.max_insts
    jitdriver = self.jitdriver

    core_id = 0
    tick_ctr = 0
    s = self.states[ core_id ]

    # use proc 0 to determine if should be running
    while self.states[0].running:

      jitdriver.jit_merge_point(
        pc        = s.fetch_pc(),
        core_id   = core_id,
        tick_ctr  = tick_ctr,
        max_insts = max_insts,
        state     = s,
        sim       = self,
      )

      # constant-fold pc and mem
      pc = hint( s.fetch_pc(), promote=True )
      old = pc
      # should be safe to constant fold memory
      mem = hint( s.mem, promote=True )

      if s.debug.enabled( "insts" ):
        print pad( "%x" % pc, 8, " ", False ),

      # the print statement in memcheck conflicts with @elidable in iread.
      # So we use normal read if memcheck is enabled which includes the
      # memory checks

      if s.debug.enabled( "memcheck" ):
        inst_bits = mem.read( pc, 4 )
      else:
        # we use trace elidable iread instead of just read
        inst_bits = mem.iread( pc, 4 )

      try:
        inst, exec_fun = self.decode( inst_bits )

        if s.debug.enabled( "insts" ):
          print "c%s %s %s %s" % (
                  core_id,
                  pad_hex( inst_bits ),
                  pad( inst.str, 12 ),
                  pad( "%d" % s.num_insts, 8 ), ),

        exec_fun( s, inst )

      except FatalError as error:
        print "Exception in execution (pc: 0x%s), aborting!" % pad_hex( pc )
        print "Exception message: %s" % error.msg
        break

      # this is the simulator's tick counter, which is total number of
      # ticks across different cores simulated
      tick_ctr += 1

      s.num_insts += 1    # TODO: should this be done inside instruction definition?
      if s.stats_en: s.stat_num_insts += 1

      # shreesha: collect instrs in serial region if stats has been enabled
      if s.stats_en and ( not s.parallel_mode ): s.serial_insts += 1

      if s.debug.enabled( "insts" ):
        print
      if s.debug.enabled( "regdump" ):
        s.rf.print_regs( per_row=4 )

      # check if we have reached the end of the maximum instructions and
      # exit if necessary
      if max_insts != 0 and s.num_insts >= max_insts:
        print "Reached the max_insts (%d), exiting." % max_insts
        break

      # switch the core
      if self.ncores > 1:
        core_id = ( core_id + 1 ) % self.ncores
        core_id = hint( core_id, promote=True )
        # here, we try switching until we find a core that's running.
        # this is an optimization when cores call the exit syscall and
        # no longer need to be ticked
        while True:
          s       = self.states[ core_id ]
          if s.running:
            break

        jitdriver.can_enter_jit(
          pc        = s.fetch_pc(),
          core_id   = core_id,
          tick_ctr  = tick_ctr,
          max_insts = max_insts,
          state     = s,
          sim       = self,
        )

    print '\nDONE! Status =', s.status
    print 'Total Ticks Simulated = %d' % tick_ctr

    # show all stats
    for i, state in enumerate( self.states ):
      print 'Core %d Instructions Executed = %d' % ( i, state.num_insts )

  #-----------------------------------------------------------------------
  # get_entry_point
  #-----------------------------------------------------------------------
  # generates and returns the entry_point function used to start the
  # simulator

  def get_entry_point( self ):
    def entry_point( argv ):

      # set the trace_limit parameter of the jitdriver
      if self.jit_enabled:
        set_param( self.jitdriver, "trace_limit", self.default_trace_limit )

      filename_idx       = 0
      debug_flags        = []
      debug_starts_after = 0
      testbin            = False
      max_insts          = 0
      envp               = []
      core_type          = 0
      stats_core_type    = 0
      accel_rf           = False

      # we're using a mini state machine to parse the args

      prev_token = ""

      # list of tokens that require an additional arg

      tokens_with_args = [ "-h", "--help",
                           "-e", "--env",
                           "-d", "--debug",
                           "--max-insts",
                           "--jit",
                           "--ncores",
                           "--core-switch-ival",
                           "--pkernel",
                           "--core-type",
                           "--stats-core-type",
                         ]

      # go through the args one by one and parse accordingly

      for i in xrange( 1, len( argv ) ):
        token = argv[i]

        if prev_token == "":

          if token == "--help" or token == "-h":
            print self.help_message % ( self.arch_name, argv[0] )
            return 0

          elif token == "--test":
            testbin = True

          elif token == "--accel-rf":
            accel_rf = True

          elif token == "--debug" or token == "-d":
            prev_token = token
            # warn the user if debugs are not enabled for this translation
            if not Debug.global_enabled:
              print "WARNING: debugs are not enabled for this translation. " + \
                    "To allow debugs, translate with --debug option."

          elif token in tokens_with_args:
            prev_token = token

          elif token[:1] == "-":
            # unknown option
            print "Unknown argument %s" % token
            return 1

          else:
            # this marks the start of the program name
            filename_idx = i
            break

        else:
          if prev_token == "--env" or prev_token == "-e":
            envp.append( token )

          elif prev_token == "--debug" or prev_token == "-d":
            # if debug start after provided (using a colon), parse it
            debug_tokens = token.split( ":" )
            if len( debug_tokens ) > 1:
              debug_starts_after = int( debug_tokens[1] )

            debug_flags = debug_tokens[0].split( "," )

          elif prev_token == "--max-insts":
            self.max_insts = int( token )

          elif prev_token == "--jit":
            # pass the jit flags to rpython.rlib.jit
            set_user_param( self.jitdriver, token )

          elif prev_token == "--ncores":
            self.ncores = int( token )

          elif prev_token == "--core-switch-ival":
            self.core_switch_ival = int( token )

          elif prev_token == "--pkernel":
            self.pkernel_bin = token

          elif prev_token == "--core-type":
            core_type = int( token )

          elif prev_token == "--stats-core-type":
            stats_core_type = int( token )

          prev_token = ""

      if filename_idx == 0:
        print "You must supply a filename"
        return 1

      # create a Debug object which contains the debug flags

      self.debug = Debug( debug_flags, debug_starts_after )

      filename = argv[ filename_idx ]

      # args after program are args to the simulated program

      run_argv = argv[ filename_idx : ]

      # Open the executable for reading

      try:
        exe_file = open( filename, 'rb' )

      except IOError:
        print "Could not open file %s" % filename
        return 1

      # Call ISA-dependent init_state to load program, initialize memory
      # etc.

      self.init_state( exe_file, filename, run_argv, envp, testbin )

      # set the core type and stats core type

      for i in range( self.ncores ):
        self.states[i].core_type = core_type
        self.states[i].stats_core_type = stats_core_type
        self.states[i].sim_ptr = self

      # set accel rf mode

      for i in range( self.ncores ):
        self.states[i].accel_rf = accel_rf

      # pass the state to debug for cycle-triggered debugging

      # TODO: not sure about this, just pass states[0]
      self.debug.set_state( self.states[0] )

      # Close after loading

      exe_file.close()

      # Execute the program

      self.run()

      return 0

    return entry_point

  #-----------------------------------------------------------------------
  # target
  #-----------------------------------------------------------------------
  # Enables RPython translation of our interpreter.

  def target( self, driver, args ):

    # if --debug flag is provided in translation, we enable debug printing

    if "--debug" in args:
      print "Enabling debugging"
      Debug.global_enabled = True
    else:
      print "Disabling debugging"

    # form a name
    exe_name = "pydgin-%s" % self.arch_name.lower()
    if driver.config.translation.jit:
      exe_name += "-jit"
    else:
      exe_name += "-nojit"

    if Debug.global_enabled:
      exe_name += "-debug"

    print "Translated binary name:", exe_name
    driver.exe_name = exe_name

    # NOTE: RPython has an assertion to check the type of entry_point to
    # generates a function type
    #return self.entry_point, None
    return self.get_entry_point(), None

#-------------------------------------------------------------------------
# init_sim
#-------------------------------------------------------------------------
# Simulator implementations need to call this function at the top level.
# This takes care of adding target function to top level environment and
# running the simulation in interpreted mode if directly called
# ( __name__ == "__main__" )

def init_sim( sim ):

  # this is a bit hacky: we get the global variables of the caller from
  # the stack frame to determine if this is being run top level and add
  # target function required by rpython toolchain

  caller_globals = sys._getframe(1).f_globals
  caller_name    = caller_globals[ "__name__" ]

  # add target function to top level

  caller_globals[ "target" ] = sim.target

  #-----------------------------------------------------------------------
  # main
  #-----------------------------------------------------------------------
  # Enables CPython simulation of our interpreter.
  if caller_name == "__main__":
    # enable debug flags in interpreted mode
    Debug.global_enabled = True
    sim.get_entry_point()( sys.argv )


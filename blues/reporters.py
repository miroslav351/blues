from mdtraj.formats.hdf5 import HDF5TrajectoryFile
from mdtraj.reporters import HDF5Reporter
from simtk.openmm import app
import simtk.unit as units
import json, yaml
import subprocess
import numpy as np
from mdtraj.utils import unitcell
from mdtraj.utils import in_units_of, ensure_type

import mdtraj.version
import simtk.openmm.version
import blues.version
import logging
import sys, time
import parmed
from blues.formats import *

from parmed import unit as u
from parmed.amber.netcdffiles import NetCDFTraj, NetCDFRestart
from parmed.geometry import box_vectors_to_lengths_and_angles
import netCDF4 as nc

def _check_mode(m, modes):
    if m not in modes:
        raise ValueError('This operation is only available when a file '
                         'is open in mode="%s".' % m)

def addLoggingLevel(levelName, levelNum, methodName=None):
    """
    Comprehensively adds a new logging level to the `logging` module and the
    currently configured logging class.

    `levelName` becomes an attribute of the `logging` module with the value
    `levelNum`. `methodName` becomes a convenience method for both `logging`
    itself and the class returned by `logging.getLoggerClass()` (usually just
    `logging.Logger`). If `methodName` is not specified, `levelName.lower()` is
    used.

    To avoid accidental clobberings of existing attributes, this method will
    raise an `AttributeError` if the level name is already an attribute of the
    `logging` module or if the method name is already present

    Example
    -------
    >>> addLoggingLevel('TRACE', logging.DEBUG - 5)
    >>> logging.getLogger(__name__).setLevel("TRACE")
    >>> logging.getLogger(__name__).trace('that worked')
    >>> logging.trace('so did this')
    >>> logging.TRACE
    5

    """
    if not methodName:
        methodName = levelName.lower()

    if hasattr(logging, levelName):
       logging.warn('{} already defined in logging module'.format(levelName))
    if hasattr(logging, methodName):
       logging.warn('{} already defined in logging module'.format(methodName))
    if hasattr(logging.getLoggerClass(), methodName):
       logging.warn('{} already defined in logger class'.format(methodName))

    # This method was inspired by the answers to Stack Overflow post
    # http://stackoverflow.com/q/2183233/2988730, especially
    # http://stackoverflow.com/a/13638084/2988730
    def logForLevel(self, message, *args, **kwargs):
        if self.isEnabledFor(levelNum):
            self._log(levelNum, message, args, **kwargs)
    def logToRoot(message, *args, **kwargs):
        logging.log(levelNum, message, *args, **kwargs)

    logging.addLevelName(levelNum, levelName)
    setattr(logging, levelName, levelNum)
    setattr(logging.getLoggerClass(), methodName, logForLevel)
    setattr(logging, methodName, logToRoot)

def init_logger(logger, level=logging.INFO, outfname=time.strftime("blues-%Y%m%d-%H%M%S")):
    fmt = LoggerFormatter()

    # Stream to terminal
    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(fmt)
    logger.addHandler(stdout_handler)

    # Write to File
    if outfname:
        fh = logging.FileHandler(outfname+'.log')
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.addHandler(logging.NullHandler())
    logger.setLevel(level)

    return logger

def getReporters(totalSteps, outfname, reporter_interval=1000, trajectory_interval=1000,
                frame_indices=[], state=True, progress=True, traj=True, restart=True, **opt):
    """
    Creates 3 OpenMM Reporters for the simulation.
    Parameters
    ----------
    totalSteps : int
        The total number of simulation steps
    reportInterval : (opt), int, default=1000
        Step frequency to write to reporter file.
    outfname : str
        Specifies the filename prefix for the reporters.
    Returns
    -------
    reporters : list of three openmm.app.simulation.reporters
        (0) state_reporter: writes energies to '.log' file.
        (1) progress_reporter: prints simulation progress to 'sys.stdout'
        (2) traj_reporter: writes trajectory to '.nc' file. AMBER NetCDF(3.0)
    """
    reporters = []
    if state:
        state_reporter = parmed.openmm.reporters.StateDataReporter(outfname+'.ene', separator="\t",
                                        reportInterval=reporter_interval,
                                        step=True,time=True,
                                        potentialEnergy=True, totalEnergy=True,
                                        volume=True, temperature=True)

        reporters.append(state_reporter)

    if progress:
        progress_reporter = parmed.openmm.reporters.ProgressReporter(outfname+'.prog', separator="\t",
                                            reportInterval=reporter_interval,
                                            totalSteps=totalSteps,
                                            potentialEnergy=True,
                                            kineticEnergy=True,
                                            totalEnergy=True,
                                            temperature=True,
                                            volume=True, step=True, time=True)
        reporters.append(progress_reporter)

    if traj:
        traj_reporter = NetCDF4Reporter(outfname+'.nc', reportInterval=reporter_interval, frame_indices=frame_indices, crds=True, vels=False, frcs=False, protocolWork=False)
        reporters.append(traj_reporter)

    if restart:
        restart_reporter = NetCDF4RestartReporter(outfname+'.rst7', reportInterval=reporter_interval, write_multiple=False, netcdf=True, write_velocities=True)
        reporters.append(restart_reporter)

    return reporters


######################
#     REPORTERS      #
######################

class BLUESHDF5Reporter(HDF5Reporter):
    #This is a subclass of the HDF5 class from mdtraj that handles the reporting of
    #the trajectory.
    @property
    def backend(self):
        return BLUESHDF5TrajectoryFile

    def __init__(self, file, reportInterval=1,
                 title='NCMC Trajectory',
                 coordinates=True, frame_indices=[],
                 time=False, cell=True, temperature=False,
                 potentialEnergy=False, kineticEnergy=False,
                 velocities=False, atomSubset=None,
                 protocolWork=True, alchemicalLambda=True,
                 parameters=None, environment=True):

        super(BLUESHDF5Reporter, self).__init__(file, reportInterval,
            coordinates, time, cell, potentialEnergy, kineticEnergy,
            temperature, velocities, atomSubset)
        self._protocolWork = bool(protocolWork)
        self._alchemicalLambda = bool(alchemicalLambda)

        self._environment = bool(environment)
        self._title = title
        self._parameters = parameters

        self.frame_indices = frame_indices
        if self.frame_indices:
            #If simulation.currentStep = 1, store the frame from the previous step.
            # i.e. frame_indices=[1,100] will store the first and frame 100
            self.frame_indices = [x-1 for x in frame_indices]

    def describeNextReport(self, simulation):
        """
        Get information about the next report this object will generate.
        Parameters
        ----------
        simulation : :class:`app.Simulation`
            The simulation to generate a report for
        Returns
        -------
        nsteps, pos, vel, frc, ene : int, bool, bool, bool, bool
            nsteps is the number of steps until the next report
            pos, vel, frc, and ene are flags indicating whether positions,
            velocities, forces, and/or energies are needed from the Context
        """
        #Monkeypatch to report at certain frame indices
        if self.frame_indices:
            if simulation.currentStep in self.frame_indices:
                steps = 1
            else:
                steps = -1
        if not self.frame_indices:
            steps_left = simulation.currentStep % self._reportInterval
            steps = self._reportInterval - steps_left
        return (steps, self._coordinates, self._velocities, False, self._needEnergy)

    def report(self, simulation, state):
        """Generate a report.
        Parameters
        ----------
        simulation : simtk.openmm.app.Simulation
            The Simulation to generate a report for
        state : simtk.openmm.State
            The current state of the simulation
        """
        if not self._is_intialized:
            self._initialize(simulation)
            self._is_intialized = True

        self._checkForErrors(simulation, state)

        args = ()
        kwargs = {}
        if self._coordinates:
            coordinates = state.getPositions(asNumpy=True)[self._atomSlice]
            coordinates = coordinates.value_in_unit(getattr(units, self._traj_file.distance_unit))
            args = (coordinates,)
        if self._time:
            kwargs['time'] = state.getTime()
        if self._cell:
            vectors = state.getPeriodicBoxVectors(asNumpy=True)
            vectors = vectors.value_in_unit(getattr(units, self._traj_file.distance_unit))
            a, b, c, alpha, beta, gamma = unitcell.box_vectors_to_lengths_and_angles(*vectors)
            kwargs['cell_lengths'] = np.array([a, b, c])
            kwargs['cell_angles'] = np.array([alpha, beta, gamma])
        if self._potentialEnergy:
            kwargs['potentialEnergy'] = state.getPotentialEnergy()
        if self._kineticEnergy:
            kwargs['kineticEnergy'] = state.getKineticEnergy()
        if self._temperature:
            kwargs['temperature'] = 2*state.getKineticEnergy()/(self._dof*units.MOLAR_GAS_CONSTANT_R)
        if self._velocities:
            kwargs['velocities'] = state.getVelocities(asNumpy=True)[self._atomSlice, :]

        #add a portion like this to store things other than the protocol work
        if self._protocolWork:
            protocol_work = simulation.integrator.get_protocol_work(dimensionless=True)
            kwargs['protocolWork'] = np.array([protocol_work])
        if self._alchemicalLambda:
            kwargs['alchemicalLambda'] = np.array([simulation.integrator.getGlobalVariableByName('lambda')])
        if self._title:
            kwargs['title'] = self._title
        if self._parameters:
            kwargs['parameters'] = self._parameters
        if self._environment:
            kwargs['environment'] = self._environment

        self._traj_file.write(*args, **kwargs)
        # flush the file to disk. it might not be necessary to do this every
        # report, but this is the most proactive solution. We don't want to
        # accumulate a lot of data in memory only to find out, at the very
        # end of the run, that there wasn't enough space on disk to hold the
        # data.
        if hasattr(self._traj_file, 'flush'):
            self._traj_file.flush()

class BLUESStateDataReporter(app.StateDataReporter):
    def __init__(self, file, reportInterval=1, frame_indices=[], title='', step=False, time=False, potentialEnergy=False, kineticEnergy=False, totalEnergy=False,   temperature=False, volume=False, density=False,
    progress=False, remainingTime=False, speed=False, elapsedTime=False, separator=',', systemMass=None, totalSteps=None):
        super(BLUESStateDataReporter, self).__init__(file, reportInterval, step, time,
            potentialEnergy, kineticEnergy, totalEnergy, temperature, volume, density,
            progress, remainingTime, speed, elapsedTime, separator, systemMass, totalSteps)

        self.log = self._out
        self.title = title

        self.frame_indices = frame_indices
        if self.frame_indices:
            #If simulation.currentStep = 1, store the frame from the previous step.
            # i.e. frame_indices=[1,100] will store the first and frame 100
            self.frame_indices = [x-1 for x in frame_indices]

    def describeNextReport(self, simulation):
        """
        Get information about the next report this object will generate.
        Parameters
        ----------
        simulation : :class:`app.Simulation`
            The simulation to generate a report for
        Returns
        -------
        nsteps, pos, vel, frc, ene : int, bool, bool, bool, bool
            nsteps is the number of steps until the next report
            pos, vel, frc, and ene are flags indicating whether positions,
            velocities, forces, and/or energies are needed from the Context
        """
        #Monkeypatch to report at certain frame indices
        if self.frame_indices:
            if simulation.currentStep in self.frame_indices:
                steps = 1
            else:
                steps = -1
        if not self.frame_indices:
            steps_left = simulation.currentStep % self._reportInterval
            steps = self._reportInterval - steps_left

        return (steps, self._needsPositions, self._needsVelocities,
                self._needsForces, self._needEnergy)

    def report(self, simulation, state):
        """Generate a report.
        Parameters
        ----------
        simulation : Simulation
            The Simulation to generate a report for
        state : State
            The current state of the simulation
        """
        if not self._hasInitialized:
            self._initializeConstants(simulation)
            headers = self._constructHeaders()
            self.log.report('#"%s"' % ('"'+self._separator+'"').join(headers))
            try:
                self._out.flush()
            except AttributeError:
                pass
            self._initialClockTime = time.time()
            self._initialSimulationTime = state.getTime()
            self._initialSteps = simulation.currentStep
            self._hasInitialized = True

        # Check for errors.
        self._checkForErrors(simulation, state)
        # Query for the values
        values = self._constructReportValues(simulation, state)

        # Write the values.
        self.log.report('%s: %s' % (self.title, self._separator.join(str(v) for v in values)))
        try:
            self._out.flush()
        except AttributeError:
            pass

class NetCDF4Reporter(parmed.openmm.reporters.NetCDFReporter):
    """ Temporary class to read or write NetCDF
    """

    def __init__(self, file, reportInterval=0, frame_indices=[], crds=True, vels=False, frcs=False,
                protocolWork=False, alchemicalLambda=False):
        super(NetCDF4Reporter,self).__init__(file, reportInterval, crds, vels, frcs)
        self.crds, self.vels, self.frcs, self.protocolWork, self.alchemicalLambda = crds, vels, frcs, protocolWork, alchemicalLambda
        self.frame_indices = frame_indices
        if self.frame_indices:
            #If simulation.currentStep = 1, store the frame from the previous step.
            # i.e. frame_indices=[1,100] will store the first and frame 100
            self.frame_indices = [x-1 for x in frame_indices]

    def describeNextReport(self, simulation):
        """
        Get information about the next report this object will generate.
        Parameters
        ----------
        simulation : :class:`app.Simulation`
            The simulation to generate a report for
        Returns
        -------
        nsteps, pos, vel, frc, ene : int, bool, bool, bool, bool
            nsteps is the number of steps until the next report
            pos, vel, frc, and ene are flags indicating whether positions,
            velocities, forces, and/or energies are needed from the Context
        """
        #Monkeypatch to report at certain frame indices
        if self.frame_indices:
            if simulation.currentStep in self.frame_indices:
                steps = 1
            else:
                steps = -1
        if not self.frame_indices:
            steps_left = simulation.currentStep % self._reportInterval
            steps = self._reportInterval - steps_left
        return (steps, self.crds, self.vels, self.frcs, False)

    def report(self, simulation, state):
        """Generate a report.
        Parameters
        ----------
        simulation : :class:`app.Simulation`
            The Simulation to generate a report for
        state : :class:`mm.State`
            The current state of the simulation
        """
        global VELUNIT, FRCUNIT
        if self.crds:
            crds = state.getPositions().value_in_unit(u.angstrom)
        if self.vels:
            vels = state.getVelocities().value_in_unit(VELUNIT)
        if self.frcs:
            frcs = state.getForces().value_in_unit(FRCUNIT)
        if self.protocolWork:
            protocolWork = simulation.integrator.get_protocol_work(dimensionless=True)
        if self.alchemicalLambda:
            alchemicalLambda = simulation.integrator.getGlobalVariableByName('lambda')
        if self._out is None:
            # This must be the first frame, so set up the trajectory now
            if self.crds:
                atom = len(crds)
            elif self.vels:
                atom = len(vels)
            elif self.frcs:
                atom = len(frcs)
            self.uses_pbc = simulation.topology.getUnitCellDimensions() is not None
            self._out = NetCDF4Traj.open_new(
                    self.fname, atom, self.uses_pbc, self.crds, self.vels,
                    self.frcs, title="ParmEd-created trajectory using OpenMM",
                    protocolWork=self.protocolWork, alchemicalLambda=self.alchemicalLambda,
            )

        if self.uses_pbc:
            vecs = state.getPeriodicBoxVectors()
            lengths, angles = box_vectors_to_lengths_and_angles(*vecs)
            self._out.add_cell_lengths_angles(lengths.value_in_unit(u.angstrom),
                                              angles.value_in_unit(u.degree))

        # Add the coordinates, velocities, and/or forces as needed
        if self.crds:
            self._out.add_coordinates(crds)
        if self.vels:
            # The velocities get scaled right before writing
            self._out.add_velocities(vels)
        if self.frcs:
            self._out.add_forces(frcs)
        if self.protocolWork:
            self._out.add_protocolWork(protocolWork)
        if self.alchemicalLambda:
            self._out.add_alchemicalLambda(alchemicalLambda)
        # Now it's time to add the time.
        self._out.add_time(state.getTime().value_in_unit(u.picosecond))

class NetCDF4RestartReporter(parmed.openmm.reporters.RestartReporter):
    """
    Use a reporter to handle writing restarts at specified intervals.
    Parameters
    ----------
    file : str
        Name of the file to write the restart to.
    reportInterval : int
        Number of steps between writing restart files
    write_multiple : bool=False
        Either write a separate restart each time (appending the step number in
        the format .# to the file name given above) if True, or overwrite the
        same file each time if False
    netcdf : bool=False
        Use the Amber NetCDF restart file format
    write_velocities : bool=True
        Write velocities to the restart file. You can turn this off for passing
        in, for instance, a minimized structure.
    """


    def __init__(self, file, reportInterval=0, frame_indices=[], write_multiple=False, netcdf=False,
                 write_velocities=True):
        super(NetCDF4RestartReporter,self).__init__(file, reportInterval, write_multiple, netcdf, write_velocities)

        self.frame_indices = frame_indices
        if self.frame_indices:
            #If simulation.currentStep = 1, store the frame from the previous step.
            # i.e. frame_indices=[1,100] will store the first and frame 100
            self.frame_indices = [x-1 for x in frame_indices]

    def describeNextReport(self, simulation):
        """
        Get information about the next report this object will generate.
        Parameters
        ----------
        simulation : :class:`app.Simulation`
            The simulation to generate a report for
        Returns
        -------
        nsteps, pos, vel, frc, ene : int, bool, bool, bool, bool
            nsteps is the number of steps until the next report
            pos, vel, frc, and ene are flags indicating whether positions,
            velocities, forces, and/or energies are needed from the Context
        """
        #Monkeypatch to report at certain frame indices
        if self.frame_indices:
            #print(self.frame_indices)
            if simulation.currentStep in self.frame_indices:
                steps = 1
            else:
                steps = -1
        if not self.frame_indices:
            steps_left = simulation.currentStep % self._reportInterval
            steps = self._reportInterval - steps_left
        return (steps, True, True, False, False)

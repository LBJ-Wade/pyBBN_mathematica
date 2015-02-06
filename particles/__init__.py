# -*- coding: utf-8 -*-
"""
= Particles =

This file contains `Particle` class definition and code governing the switching of the dynamical\
regimes
"""
from __future__ import division

import numpy

from common import GRID, PARAMS, UNITS
from common.integrators import adams_moulton_solver
from common.utils import PicklableObject

from particles import DustParticle, RadiationParticle, IntermediateParticle, NonEqParticle
# from particles.interpolation.interpolation import distribution_interpolation


class STATISTICS:
    """ == Particle species statistics == """

    @staticmethod
    @numpy.vectorize
    def Fermi(e):
        """ Fermi-Dirac:
            \begin{equation}
                \frac{1}{e^E + 1}
            \end{equation}
        """
        return 1. / (numpy.exp(e) + 1.)

    @staticmethod
    @numpy.vectorize
    def Bose(e):
        """ Bose-Einstein:
            \begin{equation}
                \frac{1}{e^E - 1}
            \end{equation}
        """
        return 1. / (numpy.exp(e) - 1.)

    BOSON = Bose
    FERMION = Fermi


class REGIMES(dict):
    """ == Particle dynamical regimes ==
        Radiation (ultra-relativistic) `RADIATION`
        :   particle mass is neglected, all values obtained analytically
        Dust (non-relativistic) `DUST`
        :   Boltzman law is used as species distribution function, simplifying the computations
        Intermediate regime `INTERMEDIATE`
        :   values are computed explicitly using the precise form of the Bose-Einstein and\
            Fermi-Dirac distributions including particle mass term
        Non-equilibrium `NONEQ`
        :   particle quantum interactions have to be computed explicitly by solving Boltzman \
            equation; individual distribution function of the species becomes utilized to obtain\
            any species-related values
        """
    RADIATION = RadiationParticle
    DUST = DustParticle
    INTERMEDIATE = IntermediateParticle
    NONEQ = NonEqParticle


class Particle(PicklableObject):

    """ == Particle ==
        Master-class for particle species. Realized as finite state machine that switches to\
        different regime when temperature becomes comparable to the particle mass or drops below\
        particle `decoupling_temperature`
    """

    _saveable_fields = [
        'name', 'symbol',
        'mass', 'decoupling_temperature',
        'dof', 'eta',
        'data',
        '_distribution', 'distribution_function',
        'collision_integral', 'collision_integrals',
        'T', 'aT'
    ]

    def __init__(self, *args, **kwargs):

        """ Set internal parameters using arguments or default values """
        self.T = PARAMS.T
        self.aT = PARAMS.aT
        self.mass = kwargs.get('mass', 0 * UNITS.eV)
        self.decoupling_temperature = kwargs.get('decoupling_temperature', 0 * UNITS.eV)
        self.name = kwargs.get('name', 'Particle')
        self.symbol = kwargs.get('symbol', 'p')

        self.dof = kwargs.get('dof', 2)  # particle species degeneracy (e.g., spin-degeneracy)

        self.statistics = kwargs.get('statistics', STATISTICS.FERMION)
        if self.statistics == STATISTICS.FERMION:
            self.eta = 1.
        else:
            self.eta = -1.

        """ For equilibrium particles distribution function is by definition given by its\
            statistics and will not be used until species comes into non-equilibrium regime """
        self._distribution = numpy.zeros(GRID.MOMENTUM_SAMPLES, dtype=numpy.float_)
        """ Particle collision integral is not effective in the equilibrium as well """
        self.collision_integral = numpy.zeros(GRID.MOMENTUM_SAMPLES, dtype=numpy.float_)

        self.collision_integrals = []
        self.data = {
            'collision_integral': []
        }

        self.update()
        self.T = PARAMS.T
        self.aT = PARAMS.aT
        self.init_distribution()

    def __str__(self):
        """ String-like representation of particle species it's regime and parameters """
        return "%s (%s, %s)\nn = %s, rho = %s\n" % (
            self.name,
            "eq" if self.in_equilibrium else "non-eq",
            self.regime.name,
            self.density() / UNITS.eV**3,
            self.energy_density() / UNITS.eV**4
        ) + ("-" * 80)

    def __repr__(self):
        return self.symbol

    def __copy__(self):
        newone = type(self)()
        newone.__dict__.update(self.__dict__)
        return newone

    def update(self, force_print=False):
        """ Update the particle parameters according to the new state of the system """
        oldregime = self.regime
        oldeq = self.in_equilibrium

        # Update particle internal params only while it is in equilibrium
        if self.in_equilibrium or self.in_equilibrium != oldeq:
            # Particle species has temperature only when it is in equilibrium
            self.T = PARAMS.T
            self.aT = PARAMS.aT

        if self.in_equilibrium != oldeq and not self.in_equilibrium:
            # Particle decouples, have to init the distribution function array for kinetics
            self.init_distribution()

        if force_print or self.regime != oldregime or self.in_equilibrium != oldeq:
            print self

    def update_distribution(self):
        """ Apply collision integral to modify the distribution function """
        if self.in_equilibrium:
            return

        self._distribution += self.collision_integral * PARAMS.dy

        # Clear collision integrands for the next computation step
        self.collision_integrals = []
        self.data['collision_integral'].append(self.collision_integral)

    def integrate_collisions(self, p0):
        """ === Particle collisions integration === """

        if not self.collision_integrals:
            return 0

        As = []
        Bs = []

        for i in self.collision_integrals:
            integral_1, _ = i.integral_1(p0)
            As.append(integral_1)

            integral_f, _ = i.integral_f(p0)
            Bs.append(integral_f)

        order = min(len(self.data['collision_integral']) + 1, 5)

        index = numpy.argwhere(GRID.TEMPLATE == p0)[0][0]
        fs = [i[index] for i in self.data['collision_integral'][-order+1:]]

        prediction = adams_moulton_solver(y=self.distribution(p0), fs=fs,
                                          A=sum(As), B=sum(Bs),
                                          h=PARAMS.dy, order=order)

        total_integral = (prediction - self.distribution(p0)) / PARAMS.dy

        return total_integral

    @property
    def regime(self):
        """
        === Regime-switching ratio ===
        For ultra-relativistic particles the mass is effectively `0`. This implies that all\
        computed numerically values can be as well obtained analytically: energy density, pressure,\
        etc.

        Let particle mass be equal $M$ and regime factor equal $\gamma$. As soon as the \
        temperature of the system $T$ drops to the value about $ M \gamma $, particle should be \
        switched to the computation regime where its mass is also considered: \
        `REGIMES.INTERMEDIATE`. When $T$ drops down even further to the value $ M / \gamma $,\
        particle species can be treated as `REGIMES.DUST` with a Boltzmann distribution function.
        """
        regime_factor = 1e1

        if not self.in_equilibrium:
            return REGIMES.NONEQ

        if self.T > self.mass * regime_factor:
            regime = REGIMES.RADIATION
        elif self.T * regime_factor < self.mass:
            regime = REGIMES.DUST
        else:
            regime = REGIMES.INTERMEDIATE

        return regime

    # TODO: probably just remove these and always use `particle.regime.something()`?
    def density(self):
        return self.regime.density(self)

    def energy_density(self):
        return self.regime.energy_density(self)

    def pressure(self):
        return self.regime.pressure(self)

    def numerator(self):
        return self.regime.numerator(self)

    def denominator(self):
        return self.regime.denominator(self)

    @property
    def distribution_function(self):
        if self.eta == -1:
            return STATISTICS.Bose
        else:
            return STATISTICS.Fermi

    def distribution(self, p):
        """
        == Distribution function interpolation ==

        Two possible strategies for the distribution function interpolation are implemented:

          * linear interpolation
          * exponential interpolation

        While linear interpolation is exceptionally simple, the exponential interpolation gives\
        exact results for the equilibrium functions - thus collision integral for them almost\
        exactly cancels out unlike the case of linear interpolation.

        Distribution functions are continuously evaluated each during the simulation, so to avoid\
        excessive evaluation of the costly logarithms and exponentials, this function first checks\
        if the current momenta value coincides with any of the grid points.
        """

        exponential_interpolation = True
        p = abs(p)
        if self.in_equilibrium or p > GRID.MAX_MOMENTUM:
            return self.distribution_function(self.conformal_energy(p) / self.aT)

        # Cython implementation experiment
        #
        # ```python
        # return distribution_interpolation(GRID.TEMPLATE, self._distribution,
        #                                 p, self.conformal_mass,
        #                                 self.eta,
        #                                 GRID.MIN_MOMENTUM, GRID.MOMENTUM_STEP)
        # ```

        greaters = numpy.where(GRID.TEMPLATE >= p)[0]
        index = greaters[0]

        if p == GRID.TEMPLATE[index]:
            return self._distribution[index]

        if index >= GRID.MOMENTUM_SAMPLES - 1:
            return self._distribution[-1]

        # Determine the closest grid points

        i_low = index
        p_low = GRID.TEMPLATE[i_low]

        i_high = index + 1
        p_high = GRID.TEMPLATE[i_high]

        if exponential_interpolation:
            """ === Exponential interpolation === """
            E_p = self.conformal_energy(p)
            E_low = self.conformal_energy(p_low)
            E_high = self.conformal_energy(p_high)

            """
            \begin{equation}
                g = \frac{ (E_p - E_{low}) g_{high} + (E_{high} - E_p) g_{low} }\
                { (E_{high} - E_{low}) }
            \end{equation}
            """

            g_high = self._distribution[i_high]
            g_low = self._distribution[i_low]

            g_high = (1. / g_high - 1.)
            if g_high > 0:
                g_high = numpy.log(g_high)

            g_low = (1. / g_low - 1.)
            if g_low > 0:
                g_low = numpy.log(g_low)

            g = ((E_p - E_low) * g_high + (E_high - E_p) * g_low) / (E_high - E_low)

            return 1. / (numpy.exp(g) + self.eta)

        else:
            """ === Linear interpolation === """
            return (
                self._distribution[i_low] * (p_high - p) + self._distribution[i_high] * (p - p_low)
            ) / (p_high - p_low)

    def equilibrium_distribution(self, p=None):
        """ Equilibrium distribution that corresponds to the particle internal temperature """
        if p is None:
            return self.distribution_function(
                numpy.vectorize(self.conformal_energy)(GRID.TEMPLATE) / self.aT
            )
        else:
            return self.distribution_function(self.conformal_energy(p) / self.aT)

    def init_distribution(self):
        self._distribution = self.equilibrium_distribution()
        return self._distribution

    @property
    def in_equilibrium(self):
        """ Simple check for equilibrium """
        return self.T > self.decoupling_temperature

    def energy(self, p):
        """ Physical energy of the particle

            \begin{equation}
                E = \sqrt{p^2 + M^2}
            \end{equation}
        """
        if self.mass > 0:
            return numpy.sqrt(p**2 + self.mass**2, dtype=numpy.float_)
        else:
            return abs(p)

    def conformal_energy(self, y):
        """ Conformal energy of the particle in comoving coordinates with evolving mass term

            \begin{equation}
                E_N = \sqrt{y^2 + (M a)^2}
            \end{equation}
        """
        if self.mass > 0:
            return numpy.sqrt(y**2 + self.conformal_mass**2)
        else:
            return abs(y)

    @property
    def conformal_mass(self):
        """ In the expanding Universe, distribution function of the massive particle in the\
            conformal coordinates changes because of the evolving mass term $M a$ """
        return self.mass * PARAMS.a

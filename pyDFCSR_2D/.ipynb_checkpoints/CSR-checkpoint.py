import numpy as np
from distgen import Generator
from lattice import *
from beams import *
from scipy.interpolate import RegularGridInterpolator
from yaml_parser import *
from tools import *
from beams import *
from lattice import *
from deposit import *
from params import *
from physical_constants import *
from r_gen6 import *
from twiss_R import *
import h5py
import os
import time
#from line_profiler_pycharm import profile
from tools import isotime
from interp3D import interpolate3D
from interp1D import interpolate1D
from numba import jit
from mpi4py import MPI


class CSR2D:
    """
    The main class to calculate 2D CSR
    """

    def __init__(self, input_file=None, parallel = False):

        self.timestamp = isotime()
        if input_file:
            self.parse_input(input_file)
            self.input_file = input_file
        self.formation_length = None
        self.initialization()  # process the initial beam

        if parallel:
            self.init_MPI()
        else:
            self.parallel = False

    def parse_input(self, input_file):
        input = parse_yaml(input_file)
        self.check_input_consistency(input)
        self.input = input
        self.beam = Beam(input['input_beam'])
        self.lattice = Lattice(input['input_lattice'])

        if 'particle_deposition' in input:
            self.DF_tracker = DF_tracker(input['particle_deposition'])
        else:
            self.DF_tracker = DF_tracker()

        if 'distribution_interpolation' in input:
            self.interpolation_params = Interpolation_params(input['distribution_interpolation'])
        else:
            self.interpolation_params = Interpolation_params()

        if 'CSR_integration' in input:
            self.integration_params = Integration_params(input['CSR_integration'])
        else:
            self.integration_params = Integration_params()

        if 'CSR_computation' in input:
            self.CSR_params = CSR_params(input['CSR_computation'])
        else:
            self.CSR_params = CSR_params()

    def initialization(self):
        """
        deposit the initial beam
        :return:
        """
        self.DF_tracker.get_DF(x=self.beam.x, z=self.beam.z, xp=self.beam.xp, t=self.beam.position)
        self.DF_tracker.append_DF()
        self.DF_tracker.append_interpolant(formation_length=float('inf'),
                                           n_formation_length=self.integration_params.n_formation_length,
                                           interpolation=self.interpolation_params)
        #Todo: add more flexible unit conversion, for both charge and energy
        self.CSR_scaling = 8.98755e3 * self.beam.charge # charge in C (8.98755e-6 MeV/m for 1nC/m^2)
        self.init_statistics()
    def init_statistics(self):
        Nstep = self.lattice.total_steps
        self.gemitX = np.zeros(Nstep)
        self.gemitX[0] = self.beam.norm_emitX


        self.slope = np.zeros((Nstep, 2))
        self.slope[0,:] = self.beam.slope

        self.Cx = np.zeros(Nstep)
        self.Cx[0] = self.beam.mean_x
        self.Cxp = np.zeros(Nstep)
        self.Cxp[0] = self.beam.mean_xp
        self.etaX = np.zeros(Nstep)
        self.etaXp = np.zeros(Nstep)
        self.betaX = np.zeros(Nstep)
        self.betaX[0] = self.beam.betaX
        self.alphaX = np.zeros(Nstep)
        self.alphaX[0] = self.beam.alphaX
        self.betaX_beam = np.zeros(Nstep)
        self.betaX_beam[0] = self.beam.betaX
        self.alphaX_beam = np.zeros(Nstep)
        self.alphaX_beam[0] = self.beam.alphaX
        emitX_t, norm_emitX_t, beta_t, alpha_t = self.beam.stats_minus_dispersion()
        self.gemitX_minus_dispersion = np.zeros(Nstep)
        self.gemitX_minus_dispersion[0] = norm_emitX_t
        self.betaX_minus_dispersion = np.zeros(Nstep)
        self.betaX_minus_dispersion[0] = beta_t
        self.alphaX_minus_dispersion = np.zeros(Nstep)
        self.alphaX_minus_dispersion[0] = alpha_t

        self.sigX = np.zeros(Nstep)
        self.sigX[0] = self.beam.sigma_x
        self.sigZ = np.zeros(Nstep)
        self.sigZ[0] = self.beam.sigma_z
        self.sigE = np.zeros(Nstep)
        self.sigE[0] = self.beam.sigma_delta

        self.R56 = np.zeros(Nstep)
        self.R51 = np.zeros(Nstep)
        self.R52 = np.zeros(Nstep)

        self.inbend = False
        self.afterbend = False
        self.R_rec = None
        self.phi_rec = None

    def init_MPI(self):
        self.parallel = True
        comm = MPI.COMM_WORLD
        self.rank = comm.Get_rank()
        mpi_size = comm.Get_size()

        work_size = self.CSR_params.xbins * self.CSR_params.zbins
        ave, res = divmod(work_size, mpi_size)
        self.count = [ave + 1 if p < res else ave for p in range(mpi_size)]
        displ = [sum(self.count[:p]) for p in range(mpi_size)]
        self.displ = np.array(displ)

    def check_input_consistency(self, input):
        # Todo: need modification if dipole_config.yaml format changed
        self.required_inputs = ['input_beam', 'input_lattice']

        allowed_params = self.required_inputs + ['particle_deposition', 'distribution_interpolation', 'CSR_integration',
                                                 'CSR_computation']
        for input_param in input:
            assert input_param in allowed_params, f'Incorrect param given to {self.__class__.__name__}.__init__(**kwargs): {input_param}\nAllowed params: {allowed_params}'

        # Make sure all required parameters are specified
        for req in self.required_inputs:
            assert req in input, f'Required input parameter {req} to {self.__class__.__name__}.__init__(**kwargs) was not found.'

    def get_formation_length(self, R, sigma_z, phi = 0.0, inbend=True):
        if inbend:
            self.formation_length = (24 * (R ** 2) * sigma_z) ** (1 / 3)
        else:
            self.formation_length = (3*R**2*phi**4)/(4*(-6*sigma_z + R*phi**3))

    #@profile
    def run(self, stop_time = None):
        Rtot6 = np.eye(6)
        step_count = 1
        betaX0 = self.beam.betaX
        alphaX0 = self.beam.alphaX
        DL = self.lattice.step_size
        ele_count = 0
        for ele in list(self.lattice.lattice_config.keys())[1:]:
            self.lattice.update(ele)
            # Todo: add sextupole, maybe Bmad Tracking?
            # -----------------------load current lattice params-----------------#
            #steps = self.lattice.lattice_config[ele]['steps']
            L = self.lattice.lattice_config[ele]['L']
            type = self.lattice.lattice_config[ele]['type']
            steps = self.lattice.steps_per_element[ele_count]
            R = float('inf')
            if type == 'dipole':
                angle = self.lattice.lattice_config[ele]['angle']
                R = L / angle
                E1 = self.lattice.lattice_config[ele]['E1']
                E2 = self.lattice.lattice_config[ele]['E2']
                dang = angle*DL/L
            if type == 'quad':
                k1 = self.lattice.lattice_config[ele]['strength']


            if type == 'dipole':
                self.inbend = True

                self.afterbend = True
                self.R_rec = R
                self.phi_rec = angle

                self.get_formation_length(R=R, sigma_z=5*self.beam.sigma_z, inbend = True)


            else:
                if self.afterbend:
                    #Todo: Verify the formation length in the drift
                    #self.get_formation_length(R=self.R_rec, sigma_z=5*self.beam.sigma_z, phi = self.phi_rec, inbend=False)
                    self.get_formation_length(R=R, sigma_z=5 * self.beam.sigma_z, inbend=True)
                else:  # if it is the first drift in the lattice
                    self.formation_length = L






            # -----------------------tracking---------------------------------
            for step in range(steps):
                time0  = time.time()
                #if type == 'dipole' and step == 6:
                #    print(ele)
                #    print("current step ", step)

                # get R6
                if type == 'dipole':
                    if step == 0:
                        dR6 = r_gen6(L=DL, angle=dang, E1=E1)
                    #Todo: How to deal with the exiting edge
                    elif step == steps - 1:
                        dR6 = r_gen6(L=DL, angle=dang, E1=0, E2=E2)
                    else:
                        dR6 = r_gen6(L=DL, angle=dang, E1=0, E2=0)
                elif type == 'drift':
                    dR6 = r_gen6(L=DL, angle  = 0)
                elif type == 'quad':
                    dR6 = r_gen6(L=DL, k1=k1)

                # Propagate beam for one step
                self.beam.track(dR6, DL)


                # get the density functions
                self.DF_tracker.get_DF(x=self.beam.x, z=self.beam.z, xp=self.beam.xp, t=self.beam.position)
                # append the density functions to the log
                self.DF_tracker.append_DF()
                # append 3D matrix for interpolation with the new DFs by interpolation
                #self.get_formation_length(R=R, sigma_z=self.beam.sigma_z)
                self.DF_tracker.append_interpolant(formation_length=self.formation_length,
                                                   n_formation_length=self.integration_params.n_formation_length,
                                                   interpolation=self.interpolation_params)
                # build interpolant based on the 3D matrix
                self.DF_tracker.build_interpolant()

                if self.CSR_params.compute_CSR:
                    if not self.parallel or self.rank == 0:
                        print('Calculating CSR at s=', str(self.beam.position))
                    if step % self.lattice.nsep[ele_count] == 0:
                        # calculate CSR mesh given beam shape
                        self.get_CSR_mesh()
                        # Calculate CSR on the mesh
                        if self.parallel:
                            self.calculate_2D_CSR_parallel()
                        else:
                            self.calculate_2D_CSR()
                        # Apply CSR kick to the beam
                        if self.CSR_params.apply_CSR:
                            self.beam.apply_wakes(self.dE_dct, self.x_kick,
                                              self.CSR_xrange_transformed, self.CSR_zrange, DL*self.lattice.nsep[ele_count])
                        if self.CSR_params.write_beam:
                            self.write_beam()
                        if self.CSR_params.write_wakes:
                            self.write_wakes()




                # recording statistics at each step
                Rtot6 = np.matmul(dR6, Rtot6)
                self.etaX[step_count] = Rtot6[0][5]
                self.etaXp[step_count] = Rtot6[1][5]
                self.R56[step_count] = Rtot6[4][5]
                self.R51[step_count] = Rtot6[4][0]
                self.R52[step_count] = Rtot6[4][1]
                self.betaX[step_count], self.alphaX[step_count], _ = twiss_R(R = Rtot6[0:2, 0:2], alpha0 = alphaX0, beta0 = betaX0)
                self.betaX_beam[step_count] = self.beam.betaX
                self.alphaX_beam[step_count] = self.beam.alphaX
                self.gemitX[step_count] = self.beam.norm_emitX
                self.Cx[step_count]  = self.beam.mean_x
                self.Cxp[step_count] = self.beam.mean_xp
                self.sigX[step_count] = self.beam.sigma_x
                self.sigZ[step_count] = self.beam.sigma_z
                self.sigE[step_count] = self.beam.sigma_delta
                self.beam_slope = self.beam.slope
                self.slope[step_count, :] = self.beam_slope

                emitX_t, norm_emitX_t, beta_t, alpha_t = self.beam.stats_minus_dispersion(Rtot= Rtot6)
                self.gemitX_minus_dispersion[step_count] = norm_emitX_t
                self.betaX_minus_dispersion[step_count] = beta_t
                self.alphaX_minus_dispersion[step_count] = alpha_t

                step_count += 1
                
                if not self.parallel or self.rank == 0:
                    print("Finish step {} in {} seconds".format(step_count, time.time() - time0))

                if stop_time and self.beam.position > stop_time:
                    return

            ele_count += 1
            

        self.write_statistics()


    def get_CSR_mesh(self):
        """
        calculating the mesh of observation points by taking linear transformation
        (xmesh, zmesh) TWO 1D arrays representiong (x, z) coordinates on a linear transformed mesh
        :return:
        """

        x_transform = self.beam.x_transform
        p = self.beam.slope

        sig_x = np.std(x_transform)
        mean_x = np.mean(x_transform)
        sig_z = self.beam.sigma_z
        mean_z = self.beam.mean_z
        xlim = self.CSR_params.xlim
        zlim = self.CSR_params.zlim
        xbins = self.CSR_params.xbins
        zbins = self.CSR_params.zbins

        zrange = np.linspace(mean_z - zlim * sig_z, mean_z + zlim * sig_z, zbins)
        xrange = np.linspace(mean_x - xlim * sig_x, mean_x + xlim * sig_x, xbins)

        # Todo: check the order
        xmesh_transform, zmesh = np.meshgrid(xrange, zrange, indexing='ij')

        xmesh_transform = xmesh_transform.flatten()
        zmesh = zmesh.flatten()

        xmesh = xmesh_transform +  np.polyval(p, zmesh)

        self.CSR_xmesh = xmesh
        self.CSR_zmesh = zmesh
        self.CSR_zrange = zrange
        self.CSR_xrange_transformed = xrange
    
    #@profile 
    def calculate_2D_CSR(self):

        N = self.CSR_params.xbins*self.CSR_params.zbins
        self.dE_dct = np.zeros((N,))
        self.x_kick = np.zeros((N,))

        start_time = time.time()
        for i in range(N):

            #if i == 210:
            #    print(i)

            #if i%int(N//10) == 0:
            #    print('Complete', str(np.round(i/N*100,2)), '%')

            s = self.beam.position + self.CSR_zmesh[i]
            x = self.CSR_xmesh[i]

            self.dE_dct[i], self.x_kick[i] = self.get_CSR_wake(s,x)

        self.dE_dct = self.dE_dct.reshape((self.CSR_params.xbins, self.CSR_params.zbins))
        self.x_kick = self.x_kick.reshape((self.CSR_params.xbins, self.CSR_params.zbins))
        print("--- %s seconds ---" % (time.time() - start_time))

    def calculate_2D_CSR_parallel(self):
        work_size= self.CSR_params.xbins * self.CSR_params.zbins
        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()
        start = int(self.displ[rank])
        local_size = int(self.count[rank])

        self.dE_dct = np.zeros((work_size,))
        self.x_kick = np.zeros((work_size,))

        dE_dct_local = np.zeros((local_size,))
        x_kick_local = np.zeros(local_size, )

        start_time = time.time()
        for i in range(local_size):
            k  = start + i
            # if i == 210:
            #    print(i)

            # if i%int(N//10) == 0:
            #    print('Complete', str(np.round(i/N*100,2)), '%')

            s = self.beam.position + self.CSR_zmesh[k]
            x = self.CSR_xmesh[k]

            dE_dct_local[i], x_kick_local[i] = self.get_CSR_wake(s, x, self.beam.sigma_x, self.beam.sigma_z)

        comm.Allgatherv(dE_dct_local, [self.dE_dct, self.count, self.displ, MPI.DOUBLE])
        comm.Allgatherv(x_kick_local, [self.x_kick, self.count, self.displ, MPI.DOUBLE])

        self.dE_dct = self.dE_dct.reshape((self.CSR_params.xbins, self.CSR_params.zbins))
        self.x_kick = self.x_kick.reshape((self.CSR_params.xbins, self.CSR_params.zbins))



    def get_localization(self, x, s, t, sp):

        X0_s = interpolate1D(xval=np.array([s]), data=self.lattice.coords[:, 0], min_x=self.lattice.min_x,
                             delta_x=self.lattice.delta_x)[0]
        X0_sp = interpolate1D(xval=sp, data=self.lattice.coords[:, 0], min_x=self.lattice.min_x,
                              delta_x=self.lattice.delta_x)
        Y0_s = interpolate1D(xval=np.array([s]), data=self.lattice.coords[:, 1], min_x=self.lattice.min_x,
                             delta_x=self.lattice.delta_x)[0]
        Y0_sp = interpolate1D(xval=sp, data=self.lattice.coords[:, 1], min_x=self.lattice.min_x,
                              delta_x=self.lattice.delta_x)
        n_vec_s_x = interpolate1D(xval=np.array([s]), data=self.lattice.n_vec[:, 0], min_x=self.lattice.min_x,
                                  delta_x=self.lattice.delta_x)[0]
        n_vec_sp_x = interpolate1D(xval=sp, data=self.lattice.n_vec[:, 0], min_x=self.lattice.min_x,
                                   delta_x=self.lattice.delta_x)
        n_vec_s_y = interpolate1D(xval=np.array([s]), data=self.lattice.n_vec[:, 1], min_x=self.lattice.min_x,
                                  delta_x=self.lattice.delta_x)[0]
        n_vec_sp_y = interpolate1D(xval=sp, data=self.lattice.n_vec[:, 1], min_x=self.lattice.min_x,
                                   delta_x=self.lattice.delta_x)
        k = interpolate1D(xval=sp, data = self.slope[:,0],min_x=self.lattice.steps_record[0],
                                   delta_x=self.lattice.step_size )

        #q_x = x*n_vec_s_x + X0_s - X0_sp
        #q_y = x*n_vec_s_y + Y0_s - Y0_sp
        #q2 = q_x*q_x + q_y*q_y

        #n_sp_q = n_vec_sp_x* q_x + n_vec_sp_y* q_y

        #term1 = (n_sp_q * k**2 + (t - sp)*k)/(k**2 - 1)

        #term2 = k**2/(k**2 - 1)*np.sqrt((k**2 - 1)*((t - sp)**2 - q2) + (n_sp_q * k + t - sp)**2)

        #xp1 = term1 + term2
        #xp2 = term1 - term2

        term = (- X0_s**2 * n_vec_sp_y**2 * k**2 + X0_s**2 +
                              2* X0_s * X0_sp * n_vec_sp_y**2 * k**2 -
                              2* X0_s * X0_sp + 2 * X0_s * Y0_s * n_vec_sp_x * n_vec_sp_y * k**2 -
                              2* X0_s * Y0_sp * n_vec_sp_x * n_vec_sp_y * k**2 -
                              2. * X0_s * n_vec_s_x * n_vec_sp_y **2 * k** 2 * x +
                              2. * X0_s * n_vec_s_x * x + 2. * X0_s * n_vec_s_y * n_vec_sp_x * n_vec_sp_y * k**2 * x -
                              2. * X0_s * n_vec_sp_x * k * sp + 2 * X0_s * n_vec_sp_x * k * t -
                              X0_sp**2 * n_vec_sp_y ** 2 * k**2 +
                              X0_sp**2 - 2* X0_sp* Y0_s * n_vec_sp_x * n_vec_sp_y * k**2 +
                              2 * X0_sp * Y0_sp * n_vec_sp_x * n_vec_sp_y * k**2 + 2 * X0_sp * n_vec_s_x * n_vec_sp_y**2 * k**2 * x -
                              2. * X0_sp * n_vec_s_x * x - 2. * X0_sp * n_vec_s_y * n_vec_sp_x * n_vec_sp_y * k**2 * x +
                              2. * X0_sp * n_vec_sp_x * k * sp - 2. * X0_sp * n_vec_sp_x * k * t -
                              Y0_s** 2 * n_vec_sp_x**2 * k**2 + Y0_s**2 + 2. * Y0_s * Y0_sp * n_vec_sp_x **2 * k**2
                              - 2. * Y0_s * Y0_sp + 2. * Y0_s * n_vec_s_x * n_vec_sp_x * n_vec_sp_y * k**2 * x -
                              2 * Y0_s * n_vec_s_y * n_vec_sp_x**2 * k**2 * x + 2. * Y0_s * n_vec_s_y * x
                              - 2. * Y0_s * n_vec_sp_y * k * sp + 2 * Y0_s * n_vec_sp_y * k * t -
                              Y0_sp**2 * n_vec_sp_x**2 * k**2 + Y0_sp ** 2 -
                              2. * Y0_sp * n_vec_s_x * n_vec_sp_x * n_vec_sp_y * k**2 * x +
                              2. * Y0_sp * n_vec_s_y * n_vec_sp_x**2 * k**2 * x -
                              2. * Y0_sp * n_vec_s_y * x + 2. * Y0_sp * n_vec_sp_y* k * sp -
                              2. * Y0_sp * n_vec_sp_y * k * t - n_vec_s_x**2 * n_vec_sp_y **2 * k**2 * x**2 +
                              n_vec_s_x**2 * x **2 + 2. * n_vec_s_x * n_vec_s_y * n_vec_sp_x * n_vec_sp_y * k**2 * x **2 -
                              2. * n_vec_s_x * n_vec_sp_x * k * sp * x + 2. * n_vec_s_x * n_vec_sp_x * k * t* x -
                              n_vec_s_y**2 * n_vec_sp_x **2 * k**2 * x** 2 + n_vec_s_y**2 * x**2 -
                              2. * n_vec_s_y * n_vec_sp_y * k * sp * x + 2 * n_vec_s_y * n_vec_sp_y * k * t * x +
                              n_vec_sp_x**2 * k**2 * sp**2 - 2. * n_vec_sp_x**2 * k**2 * sp * t +
                              n_vec_sp_x **2 * k**2 * t**2 + n_vec_sp_y**2 * k**2 * sp**2 - 2. * n_vec_sp_y**2 * k**2 * sp * t +
                              n_vec_sp_y** 2 * k**2 * t**2)** (1 / 2)

        xp1 = (k * (t - sp - term +
                              X0_s * n_vec_sp_x * k - X0_sp * n_vec_sp_x * k +
                              Y0_s * n_vec_sp_y * k - Y0_sp * n_vec_sp_y * k +
                              n_vec_s_x * n_vec_sp_x * k * x + n_vec_s_y * n_vec_sp_y * k * x)) / (
                         n_vec_sp_x**2 * k**2 + n_vec_sp_y**2 * k**2 - 1)

        xp2= (k * (t - sp + term +
                    X0_s * n_vec_sp_x * k - X0_sp * n_vec_sp_x * k +
                    Y0_s * n_vec_sp_y * k - Y0_sp * n_vec_sp_y * k +
                    n_vec_s_x * n_vec_sp_x * k * x + n_vec_s_y * n_vec_sp_y * k * x)) / (
                      n_vec_sp_x ** 2 * k ** 2 + n_vec_sp_y ** 2 * k ** 2 - 1)


        return xp1, xp2



    def get_CSR_wake(self, s, x):
        t = self.beam.position
        # -------------------------------------------------------------------------
        # Todo: kind of hard coding. Change in the future
        #start_point = self.DF_tracker.start_time
        #end_point = s + self.integration_params.zlim_end * sigma_z
        # Todo: wrong results when sigma_z is big. mid_point can smaller than start point
        #mid_point1 = s - self.integration_params.zlim_mid1 * sigma_x
        #mid_point2 = s - self.integration_params.zlim_mid2 * sigma_x
        #xlim_L = x - self.integration_params.xlim * sigma_x
        #xlim_R = x + self.integration_params.xlim * sigma_x

        #zbins_1 = self.integration_params.zbins_1
        #zbins_2 = self.integration_params.zbins_2
        #zbins_3 = self.integration_params.zbins_3
        #s_range_t1 = np.linspace(start_point, mid_point2, zbins_1)
        #s_range_t2 = np.linspace(mid_point2, mid_point1, zbins_2)
        #s_range_t3 = np.linspace(mid_point1, end_point, zbins_3)
        #s_range_t = np.concatenate((s_range_t1, s_range_t2[1:], s_range_t3[1:]))

        #x_range_t = np.linspace(xlim_L, xlim_R, self.integration_params.xbins)

        #[xp, sp] = np.meshgrid(x_range_t, s_range_t, indexing='ij')


        sp = np.linspace(np.max((s - 100 * self.beam.sigma_z, 0)), s + 100 * self.beam.sigma_z, 100000)
        xp1, xp2 = self.get_localization(x=x, s=s, t=t, sp=sp)
        ind = (np.abs(xp1) < 5 * self.beam.sigma_x) & (np.abs(xp2) < 5 * self.beam.sigma_x)

        if sum(ind) == len(xp1):  # The two region are parallel and not tilted
            xmin = x - 5 * self.beam.sigma_x
            xmax = x + 5 * self.beam.sigma_x
            smin = s - 5 * self.beam.sigma_z
            smax = s + 5 * self.beam.sigma_z

        else:
            xp1_valid = xp1[ind]
            xp2_valid = xp2[ind]
            sp_valid = sp[ind]
            smin = np.min(sp_valid)
            smax = np.max(sp_valid)
            xmin = np.min((np.min(xp1_valid), np.min(xp2_valid)))
            xmax = np.max((np.max(xp1_valid), np.max(xp2_valid)))

        sp = np.linspace(smin, smax, self.integration_params.zbins)
        xp = np.linspace(xmin, xmax, self.integration_params.xbins)
        [xp_mesh, sp_mesh] = np.meshgrid(xp, sp, indexing='ij')

        CSR_integrand_z, CSR_integrand_x = self.get_CSR_integrand(s = s, t = t, x = x, xp = xp_mesh, sp = sp_mesh)

        dE_dct1 = -self.CSR_scaling * np.trapz(y=np.trapz(y=CSR_integrand_z, x=xp, axis=0), x=sp)
        x_kick1 = self.CSR_scaling * np.trapz(y=np.trapz(y=CSR_integrand_x, x=xp, axis=0), x=sp)

        sp = np.linspace(np.max((smin - self.formation_length, 0)), smin, self.integration_params.zbins)
        xp1, xp2 = self.get_localization(x=x, s=s, t=t, sp=sp)

        xL = np.min(xp1) - 3 * self.beam.sigma_x
        xR = np.max(xp1) + 3 * self.beam.sigma_x

        xp = np.linspace(xL, xR, self.integration_params.xbins)
        [xp_mesh, sp_mesh] = np.meshgrid(xp, sp, indexing='ij')

        CSR_integrand_z, CSR_integrand_x = self.get_CSR_integrand(s = s, t = t, x = x, xp = xp_mesh, sp = sp_mesh)


        dE_dct2 = -self.CSR_scaling * np.trapz(y=np.trapz(y=CSR_integrand_z, x=xp, axis=0), x=sp)
        x_kick2 = self.CSR_scaling * np.trapz(y=np.trapz(y=CSR_integrand_x, x=xp, axis=0), x=sp)

        return dE_dct1 + dE_dct2, x_kick1 + x_kick2
        #return xp, sp, CSR_integrand_z, CSR_integrand_x
    #@profile
    def get_CSR_integrand(self,s ,x, t, sp, xp):

        #vx = self.DF_tracker.F_vx([t, x, s - t])
        vx = interpolate3D(xval=np.array([t]), yval=np.array([x]), zval=np.array([s-t]),
                             data=self.DF_tracker.data_vx_interp,
                             min_x=self.DF_tracker.min_x, min_y=self.DF_tracker.min_y,
                             min_z=self.DF_tracker.min_z,
                             delta_x=self.DF_tracker.delta_x, delta_y=self.DF_tracker.delta_y,
                             delta_z=self.DF_tracker.delta_z)[0]

        sp_flat = sp.ravel()
        xp_flat = xp.ravel()


        X0_s = interpolate1D(xval = np.array([s]), data = self.lattice.coords[:, 0], min_x = self.lattice.min_x,
                             delta_x = self.lattice.delta_x)[0]
        X0_sp = interpolate1D(xval = sp_flat, data = self.lattice.coords[:, 0], min_x = self.lattice.min_x,
                              delta_x = self.lattice.delta_x)
        Y0_s = interpolate1D(xval = np.array([s]), data = self.lattice.coords[:, 1], min_x = self.lattice.min_x,
                             delta_x = self.lattice.delta_x)[0]
        Y0_sp = interpolate1D(xval = sp_flat, data = self.lattice.coords[:, 1], min_x = self.lattice.min_x,
                              delta_x = self.lattice.delta_x)
        n_vec_s_x = interpolate1D(xval = np.array([s]), data = self.lattice.n_vec[:, 0], min_x = self.lattice.min_x,
                                  delta_x = self.lattice.delta_x)[0]
        n_vec_sp_x =interpolate1D(xval = sp_flat, data = self.lattice.n_vec[:, 0], min_x = self.lattice.min_x,
                                  delta_x = self.lattice.delta_x)
        n_vec_s_y = interpolate1D(xval=np.array([s]), data=self.lattice.n_vec[:, 1], min_x=self.lattice.min_x,
                                  delta_x=self.lattice.delta_x)[0]
        n_vec_sp_y = interpolate1D(xval=sp_flat, data=self.lattice.n_vec[:, 1], min_x=self.lattice.min_x,
                                   delta_x=self.lattice.delta_x)
        tau_vec_s_x = interpolate1D(xval=np.array([s]), data=self.lattice.tau_vec[:, 0], min_x=self.lattice.min_x,
                                  delta_x=self.lattice.delta_x)[0]
        tau_vec_sp_x = interpolate1D(xval=sp_flat, data=self.lattice.tau_vec[:, 0], min_x=self.lattice.min_x,
                                   delta_x=self.lattice.delta_x)
        tau_vec_s_y = interpolate1D(xval=np.array([s]), data=self.lattice.tau_vec[:, 1], min_x=self.lattice.min_x,
                                  delta_x=self.lattice.delta_x)[0]
        tau_vec_sp_y = interpolate1D(xval=sp_flat, data=self.lattice.tau_vec[:, 1], min_x=self.lattice.min_x,
                                   delta_x=self.lattice.delta_x)


        r_minus_rp_x = X0_s - X0_sp + x * n_vec_s_x - xp_flat * n_vec_sp_x
        r_minus_rp_y = Y0_s - Y0_sp + x * n_vec_s_y - xp_flat * n_vec_sp_y
        r_minus_rp = np.sqrt(r_minus_rp_x**2 + r_minus_rp_y**2)


        #rho_sp = self.lattice.F_rho(sp_flat)
        rho_sp = np.zeros(sp_flat.shape)
        for count in range(self.lattice.Nelement):
            if count == 0:
                rho_sp[sp_flat < self.lattice.distance[count]] = self.lattice.rho[count]
            else:
                rho_sp[(sp_flat < self.lattice.distance[count]) & (sp_flat >= self.lattice.distance[count - 1])] = self.lattice.rho[count]

        t_ret = t - r_minus_rp

        #density_ret = self.DF_tracker.F_density(np.array([t_ret, xp_flat, sp_flat - t_ret]).T)
        #density_x_ret = self.DF_tracker.F_density_x(np.array([t_ret, xp_flat, sp_flat- t_ret]).T)
        #density_z_ret = self.DF_tracker.F_density_z(np.array([t_ret, xp_flat, sp_flat- t_ret]).T)
        #vx_ret = self.DF_tracker.F_vx(np.array([t_ret, xp_flat, sp_flat- t_ret]).T)
        #vx_x_ret = self.DF_tracker.F_vx_x(np.array([t_ret, xp_flat, sp_flat- t_ret]).T)

        density_ret = interpolate3D(xval = t_ret, yval = xp_flat, zval = sp_flat - t_ret,
                                  data = self.DF_tracker.data_density_interp,
                                  min_x = self.DF_tracker.min_x, min_y = self.DF_tracker.min_y,  min_z = self.DF_tracker.min_z,
                                  delta_x = self.DF_tracker.delta_x, delta_y = self.DF_tracker.delta_y, delta_z = self.DF_tracker.delta_z)

        density_x_ret = interpolate3D(xval=t_ret, yval=xp_flat, zval=sp_flat - t_ret,
                                  data=self.DF_tracker.data_density_x_interp,
                                  min_x=self.DF_tracker.min_x, min_y=self.DF_tracker.min_y, min_z=self.DF_tracker.min_z,
                                  delta_x=self.DF_tracker.delta_x, delta_y=self.DF_tracker.delta_y,
                                  delta_z=self.DF_tracker.delta_z)

        density_z_ret = interpolate3D(xval=t_ret, yval=xp_flat, zval=sp_flat - t_ret,
                                    data=self.DF_tracker.data_density_z_interp,
                                    min_x=self.DF_tracker.min_x, min_y=self.DF_tracker.min_y,
                                    min_z=self.DF_tracker.min_z,
                                    delta_x=self.DF_tracker.delta_x, delta_y=self.DF_tracker.delta_y,
                                    delta_z=self.DF_tracker.delta_z)

        vx_ret = interpolate3D(xval=t_ret, yval=xp_flat, zval=sp_flat - t_ret,
                                    data=self.DF_tracker.data_vx_interp,
                                    min_x=self.DF_tracker.min_x, min_y=self.DF_tracker.min_y,
                                    min_z=self.DF_tracker.min_z,
                                    delta_x=self.DF_tracker.delta_x, delta_y=self.DF_tracker.delta_y,
                                    delta_z=self.DF_tracker.delta_z)

        vx_x_ret = interpolate3D(xval=t_ret, yval=xp_flat, zval=sp_flat - t_ret,
                             data=self.DF_tracker.data_vx_x_interp,
                             min_x=self.DF_tracker.min_x, min_y=self.DF_tracker.min_y,
                             min_z=self.DF_tracker.min_z,
                             delta_x=self.DF_tracker.delta_x, delta_y=self.DF_tracker.delta_y,
                             delta_z=self.DF_tracker.delta_z)

        ## Todo: More accurate vx, maybe add vs
        vs = 1
        vs_ret = 1
        vs_s_ret = 0
        vx_t = 0
        vs_t = 0
        #vx = 0
        #vx_x_ret = 0
        #vx_ret = 0

        scale_term =  1 + xp_flat*rho_sp


        velocity_x = vs * tau_vec_s_x + vx * n_vec_s_x
        velocity_y = vs * tau_vec_s_y + vx * n_vec_s_y

        velocity_ret_x = vs_ret * tau_vec_sp_x + vx_ret * n_vec_sp_x
        velocity_ret_y = vs_ret * tau_vec_sp_y + vx_ret * n_vec_sp_y

        velocity_partial_t_x = vs_t * tau_vec_sp_x + vx_t * n_vec_sp_x
        velocity_partial_t_y = vs_t * tau_vec_sp_y + vx_t * n_vec_sp_y

        nabla_density_ret_x = density_x_ret  * n_vec_sp_x + density_z_ret / scale_term * tau_vec_sp_x
        nabla_density_ret_y = density_x_ret * n_vec_sp_y + density_z_ret / scale_term * tau_vec_sp_y

        div_velocity = vs_s_ret + vx_x_ret  #???

        # Todo: Consider using general form
        ## general form
        #part1 = velocity_x * velocity_ret_x + velocity_y * velocity_ret_y
        #CSR_numerator1 = scale_term * ((velocity_x - part1 * velocity_ret_x) * nabla_density_ret_x  + \
        #                  (velocity_y - part1 * velocity_ret_y)*nabla_density_ret_y)
        #CSR_numerator2 = -scale_term * part1 * density_ret * div_velocity
        #CSR_numerator3 = scale_term * density_ret * (velocity_partial_t_x * velocity_x + velocity_partial_t_y * velocity_y)

        #CSR_denominator = r_minus_rp

        #self.CSR_integrand = CSR_numerator1/CSR_denominator + (CSR_numerator3 + CSR_numerator3)/CSR_denominator



        CSR_numerator1 = scale_term * (((n_vec_sp_x * tau_vec_s_x + n_vec_sp_y * tau_vec_s_y) +
                                        (vx - vx_ret) * (tau_vec_sp_x * tau_vec_s_x + tau_vec_sp_y * tau_vec_s_y)) * density_x_ret -
                                       vx_ret * (n_vec_sp_x * tau_vec_s_x + n_vec_sp_y * tau_vec_s_y)/scale_term * density_z_ret)

        CSR_numerator2 = -((tau_vec_sp_x * tau_vec_s_x + tau_vec_sp_y * tau_vec_s_y) +
                           (vx - vx_ret) * (n_vec_s_x * tau_vec_sp_x + n_vec_s_y * tau_vec_sp_y)) * density_ret * vx_x_ret

        CSR_numerator3 = scale_term * density_ret * (velocity_partial_t_x * velocity_x + velocity_partial_t_y * velocity_y)

        CSR_denominator = r_minus_rp

        CSR_integrand_z = CSR_numerator1/CSR_denominator + (CSR_numerator2 + CSR_numerator3)/CSR_denominator

        n_minus_np_x = n_vec_s_x - n_vec_sp_x
        n_minus_np_y = n_vec_s_y - n_vec_sp_y

        #part: (r-r')(n - n')
        part1 = r_minus_rp_x * n_minus_np_x + r_minus_rp_y * n_minus_np_y

        #part2: n tau'
        part2 = n_vec_s_x * tau_vec_sp_x + n_vec_s_y * tau_vec_sp_y

        # part3: partial density/partial t_ret
        partial_density = - (velocity_ret_x * nabla_density_ret_x + velocity_ret_y * nabla_density_ret_y) - \
                          density_ret * div_velocity

        W1 = scale_term * part1 / (r_minus_rp * r_minus_rp * r_minus_rp) * density_ret
        W2 = scale_term * part1 / (r_minus_rp * r_minus_rp) * partial_density
        W3 = -scale_term * part2 / r_minus_rp * partial_density

        CSR_integrand_x = W1 + W2 + W3

        CSR_integrand_x = CSR_integrand_x.reshape(xp.shape)
        CSR_integrand_z = CSR_integrand_z.reshape(xp.shape)



        return CSR_integrand_z, CSR_integrand_x

    def write_beam(self):
        if self.parallel and self.rank != 0:
            return

        path = full_path(self.CSR_params.workdir)
        #filename = path + '\\' + self.CSR_params.write_name + '_' + self.timestamp + '_particles.h5'
        filename = f'{path}/{self.CSR_params.write_name}-{self.timestamp}-particles.h5'
        if self.beam.step == 1:
            if os.path.isfile(filename):
                os.remove(filename)
                print("Existing file " + filename + " deleted.")
        print("Beams written to ", filename)
        with h5py.File(filename, 'a') as hf:

            step = self.beam.step
            groupname = 'step_' + str(step)
            g = hf.create_group(groupname)
            g.attrs['step'] = step
            g.attrs['position']  = self.beam.position
            g.attrs['mean_gamma'] = self.beam.init_gamma
            g.attrs['beam_energy'] = self.beam.init_energy
            g.attrs['element'] = self.lattice.current_element
            g.attrs['charge'] = self.beam.charge
            g2 = g.create_group('particles')
            g2.create_dataset('x', data = self.beam.particles[:, 0])
            g2.create_dataset('xp', data = self.beam.particles[:, 1])
            g2.create_dataset('y', data = self.beam.particles[:, 2])
            g2.create_dataset('yp', data=self.beam.particles[:, 3])
            g2.create_dataset('z', data=self.beam.particles[:,4])
            g2.create_dataset('delta', data=self.beam.particles[:, 5])

    def write_wakes(self):

        if self.parallel and self.rank != 0:
            return

        path = full_path(self.CSR_params.workdir)
        #filename = path + '\\' + self.CSR_params.write_name + '_' + self.timestamp +  '_wakes.h5'
        filename = f'{path}/{self.CSR_params.write_name}-{self.timestamp}-wakes.h5'
        print("Wakes written to ", filename)
        if self.beam.step == 1:
            if os.path.isfile(filename):
                os.remove(filename)
                print("Existing file " + filename + " deleted.")


        with h5py.File(filename, 'a') as hf:
            step = self.beam.step
            groupname = 'step_' + str(step)
            g = hf.create_group(groupname)
            g.attrs['step'] = step
            g.attrs['position']  = self.beam.position
            g.attrs['mean_gamma'] = self.beam.init_gamma
            g.attrs['beam_energy'] = self.beam.init_energy
            g.attrs['element'] = self.lattice.current_element
            g.attrs['charge'] = self.beam.charge
            g1 = g.create_group('longitudinal')
            g1.attrs['unit'] = 'MeV/m'
            g1.create_dataset('x_grids', data = self.CSR_xmesh.reshape(self.dE_dct.shape))
            g1.create_dataset('z_grids', data = self.CSR_zmesh.reshape(self.dE_dct.shape))
            g1.create_dataset('dE_dct', data = self.dE_dct)
            g2  = g.create_group('transverse')
            g2.attrs['unit'] = 'MeV/m'
            g2.create_dataset('x_grids', data = self.CSR_xmesh.reshape(self.dE_dct.shape))
            g2.create_dataset('z_grids', data = self.CSR_zmesh.reshape(self.dE_dct.shape))
            g2.create_dataset('xkicks', data = self.x_kick)


    def write_statistics(self):

        if self.parallel and self.rank != 0:
            return

        path = full_path(self.CSR_params.workdir)
        #filename = path + '\\' + self.CSR_params.write_name + '_' + self.timestamp + 'statistics.h5'
        filename = f'{path}/{self.CSR_params.write_name}-{self.timestamp}-statistics.h5'
        if os.path.isfile(filename):
            os.remove(filename)
            print("Existing file " + filename + " deleted.")
        print("Statistics written to ", filename)

        with h5py.File(filename, 'w') as hf:
            hf.create_dataset(name = 'step_positions', data = self.lattice.steps_record, shape = self.lattice.steps_record.shape)
            hf.create_dataset(name = 'slope', data = self.slope)
            hf.create_dataset(name = 'gemitX', data = self.gemitX)
            hf.create_dataset(name = 'Cx', data = self.Cx)
            hf.create_dataset(name = 'Cxp', data = self.Cxp)
            hf.create_dataset(name = 'etaX', data = self.etaX)
            hf.create_dataset(name = 'etaXp', data = self.etaXp)
            hf.create_dataset(name = 'betaX', data = self.betaX)
            hf.create_dataset(name = 'alphaX', data = self.alphaX)
            hf.create_dataset(name ='betaX_beam', data = self.betaX_beam)
            hf.create_dataset(name ='alphaX_beam', data = self.alphaX_beam)
            hf.create_dataset(name ='sigX', data = self.sigX)
            hf.create_dataset(name ='sigZ', data = self.sigZ)
            hf.create_dataset(name ='sigE', data = self.sigE)
            hf.create_dataset(name ='R56', data = self.R56)
            hf.create_dataset(name ='R51', data = self.R51)
            hf.create_dataset(name ='R52', data = self.R52)
            hf.create_dataset(name = 'gemitX_minus_dispersion', data = self.gemitX_minus_dispersion)
            hf.create_dataset(name = 'betaX_minus_dispersion', data = self.betaX_minus_dispersion)
            hf.create_dataset(name = 'alphaX_minus_dispersion', data = self.alphaX_minus_dispersion)
            hf.create_dataset(name='coords', data=self.lattice.coords)
            hf.create_dataset(name='n_vec', data=self.lattice.n_vec)
            hf.create_dataset(name='tau_vec', data=self.lattice.tau_vec)


















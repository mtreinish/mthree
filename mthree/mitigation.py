# This code is part of Mthree.
#
# (C) Copyright IBM 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
# pylint: disable=no-name-in-module
"""Calibration data"""

import warnings
from time import perf_counter

import psutil
import numpy as np
import scipy.linalg as la
import scipy.sparse.linalg as spla
import orjson
from qiskit import QuantumCircuit, transpile

from mthree.matrix import _reduced_cal_matrix, sdd_check
from mthree.utils import counts_to_vector, vector_to_quasiprobs
from mthree.norms import ainv_onenorm_est_lu, ainv_onenorm_est_iter
from mthree.matvec import M3MatVec
from mthree.exceptions import M3Error


def _tensor_meas_states(qubit, num_qubits):
    """Construct |0> and |1> states
    for marginal 1Q cals.
    """
    qc0 = QuantumCircuit(num_qubits, 1)
    qc0.measure(qubit, 0)
    qc1 = QuantumCircuit(num_qubits, 1)
    qc1.x(qubit)
    qc1.measure(qubit, 0)
    return [qc0, qc1]


def _marg_meas_states(num_qubits):
    """Construct all zeros and all ones states
    for marginal 1Q cals.
    """
    qc0 = QuantumCircuit(num_qubits)
    qc0.measure_all()
    qc1 = QuantumCircuit(num_qubits)
    qc1.x(range(num_qubits))
    qc1.measure_all()
    return [qc0, qc1]


class M3Mitigation():
    """Main M3 calibration class."""
    def __init__(self, system=None, iter_threshold=4096):
        """Main M3 calibration class.

        Parameters:
            system (BaseBackend): Target backend.
            iter_threshold (int): Sets the bitstring count at which iterative mode
                                  is turned on (assuming reasonable error rates).

        Attributes:
            system (BaseBackend): The target system.
            single_qubit_cals (list): 1Q calibration matrices
        """
        self.system = system
        self.single_qubit_cals = None
        self.num_qubits = system.configuration().num_qubits if system else None
        self.iter_threshold = iter_threshold
        self.cal_shots = None
        self.cal_method = 'marginal'
        self.rep_delay = None

    def _form_cals(self, qubits):
        """Form the 1D cals array from tensored cals data

        Parameters:
            qubits (array_like): The qubits to calibrate over.

        Returns:
            ndarray: 1D Array of float cals data.
        """
        qubits = np.asarray(qubits, dtype=int)
        cals = np.zeros(4*qubits.shape[0], dtype=float)

        # Reverse index qubits for easier indexing later
        for kk, qubit in enumerate(qubits[::-1]):
            cals[4*kk:4*kk+4] = self.single_qubit_cals[qubit].ravel()
        return cals

    def _check_sdd(self, counts, qubits, distance=None):
        """Checks if reduced A-matrix is SDD or not

        Parameters:
            counts (dict): Dictionary of counts.
            qubits (array_like): List of qubits.
            distance (int): Distance to compute over.

        Returns:
            bool: True if A-matrix is SDD, else False

        Raises:
            M3Error: Number of qubits supplied does not match bit-string length.
        """
        # If distance is None, then assume max distance.
        num_bits = len(qubits)
        if distance is None:
            distance = num_bits

        # check if len of bitstrings does not equal number of qubits passed.
        bitstring_len = len(next(iter(counts)))
        if bitstring_len != num_bits:
            raise M3Error('Bitstring length ({}) does not match'.format(bitstring_len) +
                          ' number of qubits ({})'.format(num_bits))
        cals = self._form_cals(qubits)
        return sdd_check(counts, cals, num_bits, distance)

    def tensored_cals_from_system(self, qubits=None, shots=8192,  method='independent',
                                  rep_delay=None, cals_file=None):
        """Grab calibration data from system.

        Parameters:
            qubits (array_like): Qubits over which to correct calibration data. Default is all.
            shots (int): Number of shots per circuit. Default is 8192.
            method (str): Type of calibration, 'independent' (default) or 'marginal'.
            rep_delay (float): Delay between circuits on IBM Quantum backends.
            cals_file (str): Output path to write JSON calibration data to.
        """
        warnings.warn("This method is deprecated, use 'cals_from_system' instead.")
        self.cals_from_system(qubits=qubits, shots=shots, method=method,
                              rep_delay=rep_delay,
                              cals_file=cals_file)

    def cals_from_system(self, qubits=None, shots=8192, method='independent',
                         rep_delay=None, cals_file=None):
        """Grab calibration data from system.

        Parameters:
            qubits (array_like): Qubits over which to correct calibration data. Default is all.
            shots (int): Number of shots per circuit. Default is 8192.
            method (str): Type of calibration, 'independent' (default) or 'marginal'.
            rep_delay (float): Delay between circuits on IBM Quantum backends.
            cals_file (str): Output path to write JSON calibration data to.
        """
        if qubits is None:
            qubits = range(self.num_qubits)
        self.cal_method = method
        self.rep_delay = rep_delay
        self._grab_additional_cals(qubits, shots=shots,  method=method,
                                   rep_delay=rep_delay)
        if cals_file:
            with open(cals_file, 'wb') as fd:
                fd.write(orjson.dumps(self.single_qubit_cals,
                                      option=orjson.OPT_SERIALIZE_NUMPY))

    def cals_from_file(self, cals_file):
        """Generated the calibration data from a previous runs output

            cals_file (str): A string path to the saved counts file from an
                             earlier run.
        """
        with open(cals_file, 'r') as fd:
            self.single_qubit_cals = [np.asarray(cal) if cal else None
                                      for cal in orjson.loads(fd.read())]

    def tensored_cals_from_file(self, cals_file):
        """Generated the tensored calibration data from a previous runs output

            cals_file (str): A string path to the saved counts file from an
                             earlier run.
        """
        warnings.warn("This method is deprecated, use 'cals_from_file' instead.")
        self.cals_from_file(cals_file)

    def _grab_additional_cals(self, qubits, shots=8192, method='independent', rep_delay=None):
        """Grab missing calibration data from backend.

        Parameters:
            qubits (array_like): List of measured qubits.
            shots (int): Number of shots to take.
            method (str): Type of calibration, 'independent' (default) or 'marginal'.
            rep_delay (float): Delay between circuits on IBM Quantum backends.

        Raises:
            M3Error: Backend not set.
            M3Error: Faulty qubits found.
        """
        if self.system is None:
            raise M3Error("System is not set.  Use 'cals_from_file'.")
        if self.single_qubit_cals is None:
            self.single_qubit_cals = [None]*self.num_qubits
        if self.cal_shots is None:
            self.cal_shots = shots
        if self.rep_delay is None:
            self.rep_delay = rep_delay

        if method not in ['independent', 'marginal']:
            raise M3Error('Invalid calibration method.')

        num_cal_qubits = len(qubits)
        if method == 'marginal':
            circs = _marg_meas_states(num_cal_qubits)
            trans_qcs = transpile(circs, self.system,
                                  initial_layout=qubits, optimization_level=0)
            job = self.system.run(trans_qcs, shots=self.cal_shots, rep_delay=self.rep_delay)
        else:
            circs = []
            for kk in qubits:
                circs.extend(_tensor_meas_states(kk, self.num_qubits))

            trans_qcs = transpile(circs, self.system, optimization_level=0)
            job = self.system.run(trans_qcs, shots=self.cal_shots, rep_delay=self.rep_delay)
        counts = job.result().get_counts()

        # A list of qubits with bad meas cals
        bad_list = []
        if method == 'independent':
            for idx, qubit in enumerate(qubits):
                self.single_qubit_cals[qubit] = np.zeros((2, 2), dtype=float)
                # Counts 0 has all P00, P10 data, so do that here
                prep0_counts = counts[2*idx]
                P10 = prep0_counts.get('1', 0) / shots
                P00 = 1-P10
                self.single_qubit_cals[qubit][:, 0] = [P00, P10]
                # plus 1 here since zeros data at pos=0
                prep1_counts = counts[2*idx+1]
                P01 = prep1_counts.get('0', 0) / shots
                P11 = 1-P01
                self.single_qubit_cals[qubit][:, 1] = [P01, P11]
                if P01 >= P00:
                    bad_list.append(qubit)
        else:
            prep0_counts = counts[0]
            prep1_counts = counts[1]
            for idx, qubit in enumerate(qubits):
                self.single_qubit_cals[qubit] = np.zeros((2, 2), dtype=float)
                count_vals = 0
                index = num_cal_qubits-idx-1
                for key, val in prep0_counts.items():
                    if key[index] == '0':
                        count_vals += val
                P00 = count_vals / shots
                P10 = 1-P00
                self.single_qubit_cals[qubit][:, 0] = [P00, P10]
                count_vals = 0
                for key, val in prep1_counts.items():
                    if key[index] == '1':
                        count_vals += val
                P11 = count_vals / shots
                P01 = 1-P11
                self.single_qubit_cals[qubit][:, 1] = [P01, P11]
                if P01 >= P00:
                    bad_list.append(qubit)

        if any(bad_list):
            raise M3Error('Faulty qubits detected: {}'.format(bad_list))

    def apply_correction(self, counts, qubits, distance=None,
                         method='auto',
                         max_iter=25, tol=1e-5,
                         return_mitigation_overhead=False,
                         details=False):
        """Applies correction to given counts.

        Parameters:
            counts (dict, list): Input counts dict or list of dicts.
            qubits (array_like): Qubits on which measurements applied.
            distance (int): Distance to correct for. Default=num_bits
            method (str): Solution method: 'auto', 'iterative', 'direct'.
            max_iter (int): Max. number of iterations, Default=25.
            tol (float): Convergence tolerance of iterative method, Default=1e-5.
            return_mitigation_overhead (bool): Returns the mitigation overhead, default=False.
            details (bool): Return extra info, default=False.

        Returns:
            QuasiDistribution or list: Dictionary of quasiprobabilities if
                                       input is a single dict, else a list
                                       of quasiprobabilities.

        Raises:
            M3Error: Bitstring length does not match number of qubits given.
        """
        given_list = False
        if isinstance(counts, (list, np.ndarray)):
            given_list = True
        if not given_list:
            counts = [counts]

        quasi_out = [self._apply_correction(cnts, qubits=qubits,
                                            distance=distance,
                                            method=method,
                                            max_iter=max_iter, tol=tol,
                                            return_mitigation_overhead=return_mitigation_overhead,
                                            details=details) for cnts in counts]

        if not given_list:
            return quasi_out[0]
        return quasi_out

    def _apply_correction(self, counts, qubits, distance=None,
                          method='auto',
                          max_iter=25, tol=1e-5,
                          return_mitigation_overhead=False,
                          details=False):
        """Applies correction to given counts.

        Parameters:
            counts (dict): Input counts dict.
            qubits (array_like): Qubits on which measurements applied.
            distance (int): Distance to correct for. Default=num_bits
            method (str): Solution method: 'auto', 'iterative', 'direct'.
            max_iter (int): Max. number of iterations, Default=25.
            tol (float): Convergence tolerance of iterative method, Default=1e-5.
            return_mitigation_overhead (bool): Returns the mitigation overhead, default=False.
            details (bool): Return extra info, default=False.

        Returns:
            QuasiDistribution: Dictionary of quasiprobabilities.

        Raises:
            M3Error: Bitstring length does not match number of qubits given.
        """
        # This is needed because counts is a Counts object in Qiskit not a dict.
        counts = dict(counts)
        shots = sum(counts.values())

        # If distance is None, then assume max distance.
        num_bits = len(qubits)
        num_elems = len(counts)
        if distance is None:
            distance = num_bits

        # check if len of bitstrings does not equal number of qubits passed.
        bitstring_len = len(next(iter(counts)))
        if bitstring_len != num_bits:
            raise M3Error('Bitstring length ({}) does not match'.format(bitstring_len) +
                          ' number of qubits ({})'.format(num_bits))

        # Check if no cals done yet
        if self.single_qubit_cals is None:
            warnings.warn('No calibration data. Calibrating: {}'.format(qubits))
            self._grab_additional_cals(qubits, method=self.cal_method)

        # Check if one or more new qubits need to be calibrated.
        missing_qubits = [qq for qq in qubits if self.single_qubit_cals[qq] is None]
        if any(missing_qubits):
            warnings.warn('Computing missing calibrations for qubits: {}'.format(missing_qubits))
            self._grab_additional_cals(missing_qubits, method=self.cal_method)

        if method == 'auto':
            current_free_mem = psutil.virtual_memory().available
            # First check if direct method can be run
            if num_elems <= self.iter_threshold \
                    and ((num_elems**2+num_elems)*8/1024**3 < current_free_mem/1.5):
                method = 'direct'
            # If readout is not so good try direct if memory allows
            elif np.min(self.readout_fidelity(qubits)) < 0.85 \
                    and ((num_elems**2+num_elems)*8/1024**3 < current_free_mem/1.5):
                method = 'direct'
            else:
                method = 'iterative'

        if method == 'direct':
            st = perf_counter()
            mit_counts, col_norms, gamma = self._direct_solver(counts, qubits, distance,
                                                               return_mitigation_overhead)
            dur = perf_counter()-st
            mit_counts.shots = shots
            if gamma is not None:
                mit_counts.mitigation_overhead = gamma * gamma
            if details:
                info = {'method': 'direct', 'time': dur, 'dimension': num_elems}
                info['col_norms'] = col_norms
                return mit_counts, info
            return mit_counts

        elif method == 'iterative':
            iter_count = np.zeros(1, dtype=int)

            def callback(_):
                iter_count[0] += 1

            if details:
                st = perf_counter()
                mit_counts, col_norms, gamma = self._matvec_solver(counts,
                                                                   qubits,
                                                                   distance,
                                                                   tol,
                                                                   max_iter,
                                                                   1,
                                                                   callback,
                                                                   return_mitigation_overhead)
                dur = perf_counter()-st
                mit_counts.shots = shots
                if gamma is not None:
                    mit_counts.mitigation_overhead = gamma * gamma
                info = {'method': 'iterative', 'time': dur, 'dimension': num_elems}
                info['iterations'] = iter_count[0]
                info['col_norms'] = col_norms
                return mit_counts, info
            # pylint: disable=unbalanced-tuple-unpacking
            mit_counts, gamma = self._matvec_solver(counts, qubits, distance, tol,
                                                    max_iter, 0, None,
                                                    return_mitigation_overhead)
            mit_counts.shots = shots
            if gamma is not None:
                mit_counts.mitigation_overhead = gamma * gamma
            return mit_counts

        else:
            raise M3Error('Invalid method: {}'.format(method))

    def reduced_cal_matrix(self, counts, qubits, distance=None):
        """Return the reduced calibration matrix used in the solution.

        Parameters:
            counts (dict): Input counts dict.
            qubits (array_like): Qubits on which measurements applied.
            distance (int): Distance to correct for. Default=num_bits

        Returns:
            ndarray: 2D array of reduced calibrations.
            dict: Counts in order they are displayed in matrix.

        Raises:
            M3Error: If bit-string length does not match passed number
                     of qubits.
        """
        counts = dict(counts)
        # If distance is None, then assume max distance.
        num_bits = len(qubits)
        if distance is None:
            distance = num_bits

        # check if len of bitstrings does not equal number of qubits passed.
        bitstring_len = len(next(iter(counts)))
        if bitstring_len != num_bits:
            raise M3Error('Bitstring length ({}) does not match'.format(bitstring_len) +
                          ' number of qubits ({})'.format(num_bits))

        cals = self._form_cals(qubits)
        A, counts, _ = _reduced_cal_matrix(counts, cals, num_bits, distance)
        return A, counts

    def _direct_solver(self, counts, qubits, distance=None,
                       return_mitigation_overhead=False):
        """Apply the mitigation using direct LU factorization.

        Parameters:
            counts (dict): Input counts dict.
            qubits (int): Qubits over which to calibrate.
            distance (int): Distance to correct for. Default=num_bits
            return_mitigation_overhead (bool): Returns the mitigation overhead, default=False.

        Returns:
            QuasiDistribution: dict of Quasiprobabilites
        """
        cals = self._form_cals(qubits)
        num_bits = len(qubits)
        A, sorted_counts, col_norms = _reduced_cal_matrix(counts, cals, num_bits, distance)
        vec = counts_to_vector(sorted_counts)
        LU = la.lu_factor(A, check_finite=False)
        x = la.lu_solve(LU, vec, check_finite=False)
        gamma = None
        if return_mitigation_overhead:
            gamma = ainv_onenorm_est_lu(A, LU)
        out = vector_to_quasiprobs(x, sorted_counts)
        return out, col_norms, gamma

    def _matvec_solver(self, counts, qubits, distance, tol=1e-5, max_iter=25,
                       details=0, callback=None,
                       return_mitigation_overhead=False):
        """Compute solution using GMRES and Jacobi preconditioning.

        Parameters:
            counts (dict): Input counts dict.
            qubits (int): Qubits over which to calibrate.
            tol (float): Tolerance to use.
            max_iter (int): Maximum number of iterations to perform.
            distance (int): Distance to correct for. Default=num_bits
            details (bool): Return col norms.
            callback (callable): Callback function to record iteration count.
            return_mitigation_overhead (bool): Returns the mitigation overhead, default=False.

        Returns:
            QuasiDistribution: dict of Quasiprobabilites

        Raises:
            M3Error: Solver did not converge.
        """
        cals = self._form_cals(qubits)
        M = M3MatVec(dict(counts), cals, distance)
        L = spla.LinearOperator((M.num_elems, M.num_elems),
                                matvec=M.matvec, rmatvec=M.rmatvec)
        diags = M.get_diagonal()

        def precond_matvec(x):
            out = x / diags
            return out

        P = spla.LinearOperator((M.num_elems, M.num_elems), precond_matvec)
        vec = counts_to_vector(M.sorted_counts)
        out, error = spla.gmres(L, vec, tol=tol, atol=tol, maxiter=max_iter,
                                M=P, callback=callback)
        if error:
            raise M3Error('GMRES did not converge: {}'.format(error))

        gamma = None
        if return_mitigation_overhead:
            gamma = ainv_onenorm_est_iter(M, tol=tol, max_iter=max_iter)

        quasi = vector_to_quasiprobs(out, M.sorted_counts)
        if details:
            return quasi, M.get_col_norms(), gamma
        return quasi, gamma

    def readout_fidelity(self, qubits=None):
        """Compute readout fidelity for calibrated qubits.

        Parameters:
            qubits (array_like): Qubits to compute over, default is all.

        Returns:
            list: List of qubit fidelities.

        Raises:
            M3Error: Mitigator is not calibrated.
            M3Error: Qubit indices out of range.
        """
        if self.single_qubit_cals is None:
            raise M3Error('Mitigator is not calibrated')

        if qubits is None:
            qubits = range(self.num_qubits)
        else:
            outliers = [kk for kk in qubits if kk >= self.num_qubits]
            if any(outliers):
                raise M3Error('One or more qubit indices out of range: {}'.format(outliers))
        fids = []
        for kk in qubits:
            qubit = self.single_qubit_cals[kk]
            if qubit is not None:
                fids.append(np.mean(qubit.diagonal()))
            else:
                fids.append(None)
        return fids

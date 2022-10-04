"""File containing the knitter class and associated functions."""
# This code is part of Qiskit.
#
# (C) Copyright IBM 2022.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import copy
import warnings
from typing import Set, Iterable, List, Optional, Sequence, Tuple, Union, Any, Dict
import time
from dataclasses import dataclass

import numpy as np
from nptyping import Float, Int, NDArray, Shape
import ray

from qiskit import QuantumCircuit
from qiskit.quantum_info import Pauli
from qiskit.opflow import OperatorBase, PauliSumOp
from qiskit.primitives import SamplerResult
from qiskit.providers.ibmq.job import (
    IBMQJobFailureError,
    IBMQJobApiError,
    IBMQJobInvalidStateError,
)
from qiskit_nature import QiskitNatureError
from qiskit_ibm_runtime import QiskitRuntimeService
from qiskit_ibm_runtime.sampler import SamplerResultDecoder

from .entanglement_forging_ansatz import Bitstring, EntanglementForgingAnsatz
from .entanglement_forging_operator import EntanglementForgingOperator
from ..utils import Estimator
from ..utils.grouping import (
    TPBGroupedWeightedPauliOperator,
    WeightedPauliOperator,
    to_tpb_grouped_weighted_pauli_operator,
)


class EntanglementForgingKnitter:
    """Container for Knitter class functions and attributes.

    A class which performs entanglement forging and returns the
    ground state energy and schmidt coefficients found for given
    ansatz parameters and schmidt coefficients.

    Attributes:
        - _ansatz (EntanglementForgingAnsatz): the ansatz containing the
            information for the circuit structure and bitstrings to be used
        - _backend_names (List[str]): the names of the backends to use
        - _service (QiskitRuntimeService): the service used to access backends
        - _tensor_circuits_u (List[QuantumCircuit]): the set of circuits used for the first
            operator that have the same Schmidt values
        - _superposition_circuits_u ()List[QuantumCircuit]: the set of circuits used for
            the first operator that have different Schmidt values
        - _tensor_circuits_v (List[QuantumCircuit]): the set of circuits used for the second
            operator that have the same Schmidt values
        - _superposition_circuits_v (List[QuantumCircuit]): the set of circuits used for
            the second operator that have different Schmidt values
    """

    def __init__(
        self,
        ansatz: EntanglementForgingAnsatz,
        service: Optional[QiskitRuntimeService] = None,
        backend_names: Optional[List[str]] = None,
    ):
        """
        Assign the necessary member variables.

        Args:
            - ansatz (EntanglementForgingAnsatz): The container for the circuit structure and bitstrings
                to be used (and generate the stateprep circuits)
            - service (QiskitRuntimeService): The service used to spawn Qiskit primitives and runtime jobs
            - backend_names (List[str]): Names of the backends to use for calculating expectation values

        Returns:
            - None
        """
        self._backend_names = backend_names
        self._service = service.active_account() if service is not None else service
        self._session_id: Optional[List[Optional[str]]] = None

        # Save the parameterized ansatz and bitstrings
        self._ansatz: EntanglementForgingAnsatz = EntanglementForgingAnsatz(
            circuit_u=ansatz.circuit_u,
            bitstrings_u=ansatz.bitstrings_u,
            bitstrings_v=ansatz.bitstrings_v or ansatz.bitstrings_u,
        )

        # self._tensor_circuits   = [|b1⟩,|b2⟩,...,|b2^N⟩]
        # self._superpos_circuits = [
        #           |𝜙^0_𝑏2𝑏1⟩,|𝜙^2_𝑏2𝑏1⟩,
        #           |𝜙^0_𝑏3𝑏1⟩,|𝜙^2_𝑏3𝑏1⟩,|𝜙^0_𝑏3𝑏2⟩,|𝜙^2_𝑏3𝑏2⟩
        #           |𝜙^0_𝑏4𝑏1⟩,|𝜙^2_𝑏4𝑏1⟩,|𝜙^0_𝑏4𝑏2⟩,|𝜙^2_𝑏4𝑏2⟩,|𝜙^0_𝑏4𝑏3⟩,|𝜙^2_𝑏4𝑏3⟩,
        #           ...
        #           ...,|𝜙^0_𝑏2^N𝑏(2^N-2)⟩,|𝜙^2_𝑏2^N𝑏(2^N-2)⟩,|𝜙^0_𝑏2^N𝑏(2^N-1)⟩,|𝜙^2_𝑏2^N𝑏(2^N-1)⟩]
        #
        (
            self._tensor_circuits_u,
            self._superposition_circuits_u,
        ) = _construct_stateprep_circuits(self._ansatz.bitstrings_u)
        if self._ansatz.bitstrings_are_symmetric:
            self._tensor_circuits_v, self._superposition_circuits_v = (
                self._tensor_circuits_u,
                self._superposition_circuits_u,
            )
        else:
            (
                self._tensor_circuits_v,
                self._superposition_circuits_v,
            ) = _construct_stateprep_circuits(
                self._ansatz.bitstrings_v  # type: ignore
            )

    @property
    def ansatz(self) -> EntanglementForgingAnsatz:
        """
        Property function for the ansatz.

        Args:
            - self

        Returns:
            - (EntanglementForgingAnsatz): the ansatz member variable
        """
        return self._ansatz

    @property
    def backend_names(self) -> Optional[List[str]]:
        """
        List of backend names to be used.

        Args:
            - self

        Returns:
            - (List[str]): the backend_names member variable
        """
        return self._backend_names

    @backend_names.setter
    def backend_names(self, backend_names: Optional[List[str]]) -> None:
        """
        Change the backend_names class field.

        Args:
            - self
            - backend_names (List[str]): the list of backends to use

        Returns:
            - None
        """
        self._backend_names = backend_names

    @property
    def service(self) -> Optional[QiskitRuntimeService]:
        """
        Property function for service class field.

        Args:
            - self

        Returns:
            - (QiskitRuntimeService): the service member variable
        """
        return QiskitRuntimeService(**self._service)

    @service.setter
    def service(self, service: Optional[QiskitRuntimeService]) -> None:
        """
        Change the service class field.

        Args:
            - self
            - service (QiskitRuntimeService): the service used to spawn Qiskit primitives

        Returns:
            - None
        """
        self._service = service.active_account() if service is not None else service

    def __call__(
        self,
        ansatz_parameters: Sequence[float],
        forged_operator: EntanglementForgingOperator,
    ) -> Tuple[
        float, NDArray[Shape["*"], Float], NDArray[Shape["*, *"], Float]
    ]:  # noqa: D301, D202
        """Calculate the energy.

        Computes ⟨H⟩ - the energy value and the Schmidt matrix, $h_{n, m}$, given
        some ansatz parameter values.

        $h_{n, n} = \sum_{a, b} w_{a, b} \left [ \lambda_n^2 \langle b_n | U^t P_a U | b_n \rangle
            \langle b_n | V^t P_b V | b_n \rangle \right ]$

        $h_{n, m} = \sum_{a, b} w_{a, b} \left [ \lambda_n \lambda_m \sum_{p \in 4} -1^p \langle \phi^p_{b_n, b_m}
            | U^t P_a U | \phi^p_{b_n, b_m} \rangle \langle  \phi^p_{b_n, b_m} | V^t P_b V |  \phi^p_{b_n, b_m} \rangle \right ]$

        Energy = $ \sum_{n=1}^{2^N} \left ( h_{n, n} + \sum_{m=1}^{n-1} h_{n, m} \right ) $

        For now, we are only using $p \in \{0, 2 \} $ as opposed to $ p \in \{ 0, 1, 2, 3 \} $.

        Additionally, U = V is currently required, but may change in future versions.

        Args:
            - self
            - ansatz_parameters (Sequence[float]): the parameters to be used by the ansatz circuit,
                must be the same length as the circuit's parameters
            - forged_operator (EntanglementForgingOperator): the operator to forge the expectation
                value from

        Returns:
            - (Tuple[float, NDArray[Shape["*"], Float], NDArray[Shape["*, *"], Float]]): a tuple
                containing the energy (i.e. forged expectation value), the schmidt coefficients,
                and the full schmidt decomposition matrix
        """
        # For now, we only assign the parameters to a copy of the ansatz
        circuit_u = self._ansatz.circuit_u.bind_parameters(ansatz_parameters)

        # Create the tensor and superposition stateprep circuits
        # tensor_ansatze   = [U|bi⟩      for |bi⟩       in  tensor_circuits]
        # superposition_ansatze = [U|𝜙^𝑝_𝑏𝑛𝑏𝑚⟩ for |𝜙^𝑝_𝑏𝑛𝑏𝑚⟩ in superposition_circuits]
        tensor_ansatze_u = [
            prep_circ.compose(circuit_u) for prep_circ in self._tensor_circuits_u
        ]
        superposition_ansatze_u = [
            prep_circ.compose(circuit_u) for prep_circ in self._superposition_circuits_u
        ]

        tensor_ansatze_v = []
        superposition_ansatze_v = []
        if not self._ansatz.bitstrings_are_symmetric:
            tensor_ansatze_v = [
                prep_circ.compose(circuit_u) for prep_circ in self._tensor_circuits_v
            ]
            superposition_ansatze_v = [
                prep_circ.compose(circuit_u)
                for prep_circ in self._superposition_circuits_v
            ]

        # Partition the expectation values for parallel calculation
        if self._backend_names:
            num_partitions = len(self._backend_names)
        else:
            num_partitions = 1

        if self._session_id is None:
            self._session_id = [None] * num_partitions

        tensor_ansatze = tensor_ansatze_u + tensor_ansatze_v
        superposition_ansatze = superposition_ansatze_u + superposition_ansatze_v

        partitioned_tensor_ansatze = _partition(tensor_ansatze, num_partitions)
        partitioned_superposition_ansatze = _partition(
            superposition_ansatze, num_partitions
        )

        partitioned_expval_futures = [
            _estimate_expvals.remote(  # type: ignore
                tensor_ansatze=tensor_ansatze_partition,
                tensor_paulis=forged_operator.tensor_paulis,
                superposition_ansatze=superposition_ansatze_partition,
                superposition_paulis=forged_operator.superposition_paulis,
                service=self._service,
                backend_names=self._backend_names,
                backend_index=partition_index,
                session_id=self._session_id[partition_index],
            )
            for partition_index, (
                tensor_ansatze_partition,
                superposition_ansatze_partition,
            ) in enumerate(
                zip(partitioned_tensor_ansatze, partitioned_superposition_ansatze)
            )
        ]

        tensor_expvals = []
        superposition_expvals = []
        for i, partition_expval_futures in enumerate(partitioned_expval_futures):
            (
                partition_tensor_expvals,
                partition_superposition_expvals,
                self._session_id[i],
            ) = ray.get(partition_expval_futures)
            tensor_expvals.extend(partition_tensor_expvals)
            superposition_expvals.extend(partition_superposition_expvals)

        # Compute the Schmidt matrix
        h_schmidt = self._compute_h_schmidt(
            forged_operator, np.array(tensor_expvals), np.array(superposition_expvals)
        )
        evals, evecs = np.linalg.eigh(h_schmidt)
        schmidt_coeffs = evecs[:, 0]
        energy = evals[0]

        return energy, schmidt_coeffs, h_schmidt

    def _compute_h_schmidt(
        self,
        forged_operator: EntanglementForgingOperator,
        tensor_expvals: NDArray[Shape["*, *"], Float],
        superpos_expvals: NDArray[Shape["*, *"], Float],
    ) -> NDArray[Shape["*, *"], Float]:  # noqa: D202
        """
        Compute the Schmidt decomposition of the Hamiltonian.

        Args:
            - forged_operator (EntanglementForgingOperator): the operator that the
                forged expectation values are computed with
            - tensor_expvals (NDArray[Shape["*, *"], Float]): the expectation values
                for the tensor circuits (i.e. same Schmidt coefficients)
            - superpos_expvals (NDArray[Shape["*, *"], Float]): the expectation values
                for the superposition circuits (i.e. different Schmidt coefficients)

        Returns:
           - (NDArray[Shape["*, *"], Float]): the Schmidt matrix
        """

        # Calculate the diagonal entries of the Schmidt matrix by
        # summing the expectation values associated with the tensor terms
        # h𝑛𝑛 = Σ_ab 𝑤𝑎𝑏•[ 𝜆𝑛^2•⟨b𝑛|U^t•P𝑎•U|b𝑛⟩⟨b𝑛|V^t•P𝑏•V|b𝑛⟩ ]
        if self._ansatz.bitstrings_are_symmetric:
            h_schmidt_diagonal = np.einsum(
                "ij, xi, xj->x",
                forged_operator.w_ij,  # type: ignore
                tensor_expvals,
                tensor_expvals,
            )
        else:
            num_tensor_terms = int(np.shape(tensor_expvals)[0] / 2)
            h_schmidt_diagonal = np.einsum(
                "ij, xi, xj->x",
                forged_operator.w_ij,  # type: ignore
                tensor_expvals[:num_tensor_terms, :],
                tensor_expvals[num_tensor_terms:, :],
            )
        h_schmidt = np.diag(h_schmidt_diagonal)

        # Including the +/-Y superpositions would increase this to 4
        num_lin_combos = 2

        # superpos_ansatze[2i]   = U|𝜙^0_𝑏𝑛𝑏𝑚⟩
        # superpos_expvals[2i]   = [⟨𝜙^0_𝑏𝑛𝑏𝑚|U^t•𝑃𝑎•U|𝜙^0_𝑏𝑛𝑏𝑚⟩ for 𝑃𝑎 in superpos_paulis]
        # superpos_expvals[2i+1] = [⟨𝜙^1_𝑏𝑛𝑏𝑚|U^t•𝑃𝑎•U|𝜙^1_𝑏𝑛𝑏𝑚⟩ for 𝑃𝑎 in superpos_paulis]
        superpos_expvals = np.array(superpos_expvals)

        if self._ansatz.bitstrings_are_symmetric:
            p_plus_x = superpos_expvals[0::num_lin_combos, :]
            p_minus_x = superpos_expvals[1::num_lin_combos, :]
            p_delta_x_u = p_plus_x - p_minus_x
            p_delta_x_v = p_delta_x_u
        else:
            num_superpos_terms = int(np.shape(superpos_expvals)[0] / 2)
            pvss_u = superpos_expvals[:num_superpos_terms, :]
            pvss_v = superpos_expvals[num_superpos_terms:, :]

            p_plus_x_u = pvss_u[0::num_lin_combos, :]
            p_minus_x_u = pvss_u[1::num_lin_combos, :]
            p_delta_x_u = p_plus_x_u - p_minus_x_u

            p_plus_x_v = pvss_v[0::num_lin_combos, :]
            p_minus_x_v = pvss_v[1::num_lin_combos, :]
            p_delta_x_v = p_plus_x_v - p_minus_x_v

        # Calculate and assign the off-diagonal values of the Schmidt matrix by
        # summing the expectation values associated with the superpos terms
        h_schmidt_off_diagonals = np.einsum(
            "ab,xa,xb->x", forged_operator.w_ab, p_delta_x_u, p_delta_x_v  # type: ignore
        )
        # Create off diagonal index list
        superpos_indices = []
        for x in range(self._ansatz.subspace_dimension):
            for y in range(self._ansatz.subspace_dimension):
                if x == y:
                    continue
                superpos_indices += [(x, y)]

        # h𝑛𝑚 = Σ_ab 𝑤𝑎𝑏 •[ 𝜆𝑛𝜆𝑚•Σ_𝑝∈ℤ4 -1^𝑝•⟨𝜙^𝑝_𝑏𝑛𝑏𝑚|U^t•𝑃𝑎•U|𝜙^𝑝_𝑏𝑛𝑏𝑚⟩•
        #                                   ⟨𝜙^𝑝_𝑏𝑛𝑏𝑚|V^t•𝑃𝑏•V|𝜙^𝑝_𝑏𝑛𝑏𝑚⟩ ]
        for element, indices in zip(h_schmidt_off_diagonals, superpos_indices):
            h_schmidt[indices] = element

        return h_schmidt


def _construct_stateprep_circuits(
    bitstrings: List[Bitstring],
    subsystem_id: Optional[str] = None,
) -> Tuple[List[QuantumCircuit], List[QuantumCircuit]]:  # noqa: D301
    """Prepare all circuits.

    Function to make the state preparation circuits. This constructs a set
    of circuits $ | b_n \rangle $ and $ | \phi^{p}_{n, m} \rangle $.

    The circuits $ | b_n \rangle $ are computational basis states specified by
    bitstrings $ b_n $, while the circuits $ | \phi^{p}_{n, m} \rangle $ are
    superpositions over pairs of bitstrings:

    $ | \phi^{p}_{n, m} \rangle = (| b_n \rangle + i^p | b_m \rangle) / \sqrt{2} $,
    as defined in <https://arxiv.org/abs/2104.10220>. Note that the output
    scaling (for the square root) is done in the estimator function.

    Example:
    _construct_stateprep_circuits([[0, 1], [1, 0]]) yields:

    bs0
    q_0: ─────
         ┌───┐
    q_1: ┤ X ├
         └───┘
    bs1
         ┌───┐
    q_0: ┤ X ├
         └───┘
    q_1: ─────

    bs0bs1xplus
         ┌───┐
    q_0: ┤ H ├──■──
         ├───┤┌─┴─┐
    q_1: ┤ X ├┤ X ├
         └───┘└───┘
    bs0bs1xmin
         ┌───┐┌───┐
    q_0: ┤ H ├┤ Z ├──■──
         ├───┤└───┘┌─┴─┐
    q_1: ┤ X ├─────┤ X ├
         └───┘     └───┘
    bs1bs0xplus
         ┌───┐
    q_0: ┤ H ├──■──
         ├───┤┌─┴─┐
    q_1: ┤ X ├┤ X ├
         └───┘└───┘
    bs1bs0xmin
         ┌───┐┌───┐
    q_0: ┤ H ├┤ Z ├──■──
         ├───┤└───┘┌─┴─┐
    q_1: ┤ X ├─────┤ X ├
         └───┘     └───┘

    Args:
        - bitstrings (List[Bitstring]): the input list of bitstrings used to generate the state preparation circuits
        - subsystem_id (Optional[str]): the subsystem the bitstring reflects ("u" or "v")

    Returns:
        - (Tuple[List[QuantumCircuit], List[QuantumCircuit]]): A tuple containing the tensor (i.e., non-superposition
            or bitstring) circuits in the first index (length = len(bitstrings)) and the super-position circuits
            as the second element
    """
    # If empty, just return
    if not bitstrings:
        return [], []

    if subsystem_id is None:
        subsystem_id = "u"
    # If the spin-up and spin-down spin orbitals are together a 2*N qubit system,
    # the bitstring should be N bits long.
    bitstring_array = np.asarray(bitstrings)
    tensor_prep_circuits = [
        _prepare_bitstring(bs, name=f"bs{subsystem_id}{str(bs_idx)}")
        for bs_idx, bs in enumerate(bitstring_array)
    ]

    superpos_prep_circuits = []
    # Create superposition circuits for each bitstring pair
    for bs1_idx, bs1 in enumerate(bitstring_array):
        for bs2_idx, bs2 in enumerate(bitstring_array):
            if bs1_idx == bs2_idx:
                continue
            diffs = np.where(bs1 != bs2)[0]
            if len(diffs) > 0:
                i = diffs[0]
                if bs1[i]:
                    x = bs2
                    y = bs1
                else:
                    x = bs1
                    y = bs2

                # Find the first position the bitstrings differ and place a
                # hadamard in that position
                S = np.delete(diffs, 0)
                qcirc = _prepare_bitstring(np.concatenate((x[:i], [0], x[i + 1 :])))
                qcirc.h(i)

                # Create a superposition circuit for each psi value in {0, 2}
                psi_xplus, psi_xmin = [
                    qcirc.copy(
                        name=f"bs{subsystem_id}{bs1_idx}bs{subsystem_id}{bs2_idx}{name}"
                    )
                    for name in ["xplus", "xmin"]
                ]
                psi_xmin.z(i)
                for psi in [psi_xplus, psi_xmin]:
                    for target in S:
                        psi.cx(i, target)
                    superpos_prep_circuits.append(psi)

            # If the two bitstrings are equivalent (i.e. bn==bm)
            else:
                qcirc = _prepare_bitstring(
                    bs1,
                    name=f"bs{subsystem_id}{bs1_idx}bs{subsystem_id}{bs2_idx}_hybrid_",
                )
                psi_xplus, psi_xmin = [
                    qcirc.copy(name=f"{qcirc.name}{name}") for name in ["xplus", "xmin"]
                ]
                superpos_prep_circuits += [psi_xplus, psi_xmin]

    return tensor_prep_circuits, superpos_prep_circuits


def _prepare_bitstring(
    bitstring: Union[NDArray[Shape["*"], Int], Bitstring],
    name: Optional[str] = None,
) -> QuantumCircuit:
    """Prepare the bitstring circuits.

    Generate a computational basis state from the input bitstring by applying an X gate to
    every qubit that has a 1 in the bitstring.

    Args:
        - bitstring (Union[NDArray[Shape["*"], Int], Bitstring]): the container for the
            bitstring information. Must contain 0s and 1s and the 1s are used to determine
            where to put the X gates
        - name (str, optional): the name of the circuit

    Returns:
        - (QuantumCircuit): the prepared circuit
    """
    qcirc = QuantumCircuit(len(bitstring), name=name)
    for qb_idx, bit in enumerate(bitstring):
        if bit:
            qcirc.x(qb_idx)
    return qcirc


def _partition(a, n):
    """Partitions the input.

    Function that partitions the input, a, into a generator containing
    n sub-partitions of a (that are the same type as a).
    Example:
    _partition([1, 2, 3], 2) -> (i for i in [[1, 2], [3]])

    Args:
        - a (iterable): an object with length and indexing to be partitioned
        - n (int): the number of partitions
    Returns:
        - (generator): the generator containing the paritions
    """
    k, m = divmod(len(a), n)
    return (a[i * k + min(i, m) : (i + 1) * k + min(i + 1, m)] for i in range(n))


@ray.remote
def _estimate_expvals(
    tensor_ansatze: List[QuantumCircuit],
    tensor_paulis: List[Pauli],
    superposition_ansatze: List[QuantumCircuit],
    superposition_paulis: List[Pauli],
    service: Optional[Dict[str, Any]] = None,
    backend_names: Optional[List[str]] = None,
    backend_index: int = 0,
    session_id: Optional[str] = None,
) -> Tuple[List[NDArray], List[NDArray], Optional[str]]:
    """Run quantum circuits to generate the expectation values.

    Function to estimate the exepctation value of some observables on the
    tensor and superposition circuits used for reconstructing the full
    expectation value from the Schmidt decomposed circuit. The ray decorator
    indicates that this is an actor function (that runs its own python
    process).

    Args:
        - tensor_ansatze (List[QuantumCircuit]): the circuits that have the same
            Schmidt coefficient
        - tensor_paulis (List[Pauli]): the pauli operators to measure and calculate
            the expectation values from for the circuits with the same Schmidt coefficient
        - superposition_ansatze (List[QuantumCircuit]): the circuits with different
            Schmidt coefficients
        - superposition_paulis (List[Pauli]): the pauli operators to measure and calculate
            the expectation values from for the circuits with different Schmidt
            coefficients
        - service (Dict[str, Any]): The service account used to spawn Qiskit primitives
        - backend_names (List[str]): The list of backends to use to evaluate the grouped experiments
        - backend_index (int): The index of the backend to be used
        - session_id (str): The session id to use when calling primitive programs

    Returns:
        - (Tuple[List[NDArray], List[NDArray]]): the expectation values for the
            tensor circuits and superposition circuits
    """
    service = QiskitRuntimeService(**service) if service is not None else None

    if backend_names and not service:
        raise ValueError("A service must be specified to use specific backends.")

    # Get names of Paulis in each basis
    tensor_pauli_names = [pauli.to_label() for pauli in tensor_paulis]
    superposition_pauli_names = [pauli.to_label() for pauli in superposition_paulis]

    # Create the grouped operators
    grouping_tensor_op = to_tpb_grouped_weighted_pauli_operator(
        WeightedPauliOperator(
            paulis=[[1, Pauli(pname)] for pname in tensor_pauli_names]
        ),
        TPBGroupedWeightedPauliOperator.sorted_grouping,
    )
    grouping_superposition_op = to_tpb_grouped_weighted_pauli_operator(
        WeightedPauliOperator(
            paulis=[[1, Pauli(pname)] for pname in superposition_pauli_names]
        ),
        TPBGroupedWeightedPauliOperator.sorted_grouping,
    )

    tensor_circuits_to_execute = _prepare_circuits_to_execute(
        tensor_ansatze, grouping_tensor_op
    )
    superposition_circuits_to_execute = _prepare_circuits_to_execute(
        superposition_ansatze, grouping_superposition_op
    )

    # If a service was passed, expectation values will be calculated using grouping
    if service:
        if backend_names is None:
            raise ValueError(
                "A service was passed but no backend names were specified."
            )
        all_circuits = tensor_circuits_to_execute + superposition_circuits_to_execute
        num_shots = 1024
        inputs = {
            "circuits": all_circuits,
            "circuit_indices": list(range(len(all_circuits))),
            "shots": num_shots,
            "transpilation_options": {"optimization_level": 3},
            "resilience_settings": {"level": 1},
        }
        options = {"backend": backend_names[backend_index]}

        start_session = False
        if session_id is None:
            start_session = True

        results, job_id = _execute_with_retry(
            service=service,
            inputs=inputs,
            options=options,
            session_id=session_id,
            start_session=start_session,
        )

        if session_id is None:
            session_id = job_id

        # Split the results back out into tensor and superposition results
        tensor_result = {}
        tensor_result["quasi_dists"] = [
            results.quasi_dists[i] for i, _ in enumerate(tensor_circuits_to_execute)
        ]
        tensor_result["metadata"] = [
            {"name": circ.name, "n_qubits": circ.num_qubits, "shots": num_shots}
            for circ in tensor_circuits_to_execute
        ]

        superposition_result = {}
        num_tensor_circuits = len(tensor_circuits_to_execute)
        superposition_result["quasi_dists"] = [
            results.quasi_dists[i + num_tensor_circuits]
            for i, _ in enumerate(superposition_circuits_to_execute)
        ]
        superposition_result["metadata"] = [
            {"name": circ.name, "n_qubits": circ.num_qubits, "shots": num_shots}
            for circ in superposition_circuits_to_execute
        ]

        # Calculate the inferred expectation values, given the results from the grouped experiments
        tensor_name_prefixes = [circ.name for circ in tensor_ansatze]
        tensor_expvals = _get_expectation_values_from_counts(
            tensor_result, tensor_name_prefixes, grouping_tensor_op
        )
        tensor_expval_list = list(tensor_expvals)
        superposition_name_prefixes = [circ.name for circ in superposition_ansatze]
        superposition_expvals = _get_expectation_values_from_counts(
            superposition_result, superposition_name_prefixes, grouping_superposition_op
        )
        superposition_expval_list = list(superposition_expvals)

    # If no service was passed, use local Estimator for expval calculation
    else:
        with Estimator(
            circuits=(tensor_ansatze + superposition_ansatze),
            observables=(tensor_paulis + superposition_paulis),
        ) as estimator:
            # Get the indices for the tensor experiments
            ansatz_indices_t: List[int] = []
            observable_indices_t: List[int] = []
            for i, _ in enumerate(tensor_ansatze):
                ansatz_indices_t += [i] * len(tensor_paulis)
                observable_indices_t += range(len(tensor_paulis))

            # Get the indices and scalars for the superposition experiments
            ansatz_indices_s: List[int] = []
            observable_indices_s: List[int] = []
            for i, ansatz in enumerate(superposition_ansatze):
                # superposition_ansatze[i] = U|𝜙^𝑝_𝑏𝑛𝑏𝑚⟩
                ansatz_indices_s += [len(tensor_ansatze) + i] * len(
                    superposition_paulis
                )
                observable_indices_s += range(
                    len(tensor_paulis), len(tensor_paulis) + len(superposition_paulis)
                )

            # Get all expectation values in one call to the estimator
            ansatz_indices = ansatz_indices_t + ansatz_indices_s
            observable_indices = observable_indices_t + observable_indices_s

            # estimator_results = [⟨bi|U^t•P0•U|bi⟩,⟨bi|U^t•P1•U|bi⟩, ....]
            estimator_results = estimator(ansatz_indices, observable_indices).values

            # Post-process the results to get our expectation values in the right format
            num_tensor_expvals = len(tensor_ansatze) * len(tensor_paulis)
            estimator_results_t = estimator_results[:num_tensor_expvals]
            estimator_results_s = estimator_results[num_tensor_expvals:]
            # tensor_expvals[𝑛][𝑎] = ⟨b𝑛|U^t•P0•U|b𝑛⟩
            tensor_expval_list = list(
                estimator_results_t.reshape((len(tensor_ansatze), len(tensor_paulis)))
            )
            superposition_expval_list = list(
                estimator_results_s.reshape(
                    (len(superposition_ansatze), len(superposition_paulis))
                )
            )

    # Scale the superposition terms
    for i, ansatz in enumerate(superposition_ansatze):
        # Scale the expectation values to account for 1/sqrt(2) coefficients
        if "hybrid_xmin" in ansatz.name:
            superposition_expval_list[i] *= 0.0
        elif "hybrid_xplus" in ansatz.name:
            pass
        else:
            superposition_expval_list[i] *= 0.5

    return tensor_expval_list, superposition_expval_list, session_id


def _get_expectation_values_from_counts(
    counts: Dict[str, List[Dict[str, Any]]],
    stateprep_strings: List[str],
    grouping_operator: WeightedPauliOperator,
) -> NDArray[Shape["*, *"], Float]:
    """Calculate expectation values of Pauli strings evaluated for various wavefunctions.

    Args:
        - counts: Dictionary containing the shot counts and metadata
        - stateprep_strings: List of ansatz circuit names, which are keys into the counts dict
        - grouping_operator: The grouping operator used to compress the Pauli basis
            Schmidt coefficient

    Returns:
        - Tuple[ndarray, ndarray]: the expectation values for the
            tensor circuits and superposition circuits.
            Shape is (num_ansatze, num_paulis)
    """
    pauli_vals = np.zeros(
        (
            len(stateprep_strings),
            len(grouping_operator._paulis),
        )
    )
    pauli_names_temp = [p[1].to_label() for p in grouping_operator.paulis]
    for prep_idx, prep_string in enumerate(stateprep_strings):
        suffix = prep_string[2]
        if suffix not in ["u", "v"]:
            raise ValueError(f"Invalid stateprep circuit name: {prep_string}")
        bitstring_pair = [0, 0]
        tensor_circuit = True
        num_bs_terms = prep_string.count("bs")
        if (num_bs_terms > 2) or (num_bs_terms == 0):
            raise ValueError(f"Invalid stateprep circuit name: {prep_string}")
        elif num_bs_terms == 2:
            tensor_circuit = False

        pauli_vals_temp, _ = _eval_each_pauli_with_counts(
            grouping_pauli_operator=grouping_operator,
            counts=counts,
            circuit_name_prefix=prep_string + "_",
        )

        pauli_vals_alphabetical = [
            x[1] for x in sorted(list(zip(pauli_names_temp, pauli_vals_temp)))
        ]
        if not np.all(np.isreal(pauli_vals_alphabetical)):
            warnings.warn(
                "Computed Pauli expectation value has nonzero "
                "imaginary part which will be discarded."
            )
        pauli_vals[prep_idx, :] = np.real(pauli_vals_alphabetical)

    return pauli_vals


def _eval_each_pauli_with_counts(
    grouping_pauli_operator: WeightedPauliOperator,
    counts: Dict[str, List[Dict[str, Any]]],
    circuit_name_prefix: str = "",
) -> Tuple[NDArray, NDArray]:
    """Return inferred expectation values for all Paulis within a group.

    Args:
        - grouping_operator: The grouping operator used to compress the Pauli basis
            Schmidt coefficient
        - counts: Dictionary containing the shot counts and metadata
        - circuit_name_prefix: Name of ansatz, used to index into counts dict

    Returns:
        - Tuple[ndarray, ndarray]: the inferred means and covariances for all
        Paulis within the group.
    """
    if grouping_pauli_operator.is_empty():
        raise QiskitNatureError("Operator is empty, check the operator.")
    num_paulis = len(grouping_pauli_operator._paulis)
    means = np.zeros(num_paulis)
    cov = np.zeros((num_paulis, num_paulis))

    # Make a counts dict
    counts_dict = {}
    num_qubits = counts["metadata"][0]["n_qubits"]
    for i, dist in enumerate(counts["quasi_dists"]):
        tmp_counts = {}
        for bitstring in dist.keys():
            # bitstring = str(bin(int(value, 16)))[2:].zfill(num_qubits)
            tmp_counts[bitstring] = round(
                dist[bitstring] * counts["metadata"][0]["shots"]
            )
        counts_dict[counts["metadata"][i]["name"]] = tmp_counts

    for basis, p_indices in grouping_pauli_operator._basis:
        circ_counts = counts_dict[circuit_name_prefix + basis.to_label()]
        paulis = [grouping_pauli_operator._paulis[idx] for idx in p_indices]
        paulis = [p[1] for p in paulis]  # Discarding the weights
        means_this_basis, cov_this_basis = _compute_pauli_means_and_cov_for_one_basis(
            paulis, circ_counts
        )
        for p_idx, p_mean in zip(p_indices, means_this_basis):
            means[p_idx] = p_mean
        cov[np.ix_(p_indices, p_indices)] = cov_this_basis
    return means, cov


def _prepare_circuits_to_execute(
    ansatze: List[QuantumCircuit],
    grouping_operator: WeightedPauliOperator,
) -> List[QuantumCircuit]:
    """Return all unique circuits that must be run to evaluate the ansatze on the operator.

    Args:
        - ansatze: List of ansatze for which we need to calculate expectation values
        - grouping_operator: The grouping operator used to compress the Pauli basis
            Schmidt coefficient

    Returns:
        - List[QuantumCircuit]: A list of unique QuantumCircuits which must be evaluated
    """
    circuits_to_execute = []
    # Generate the requisite circuits:
    for prep_circ in [qc.copy() for qc in ansatze]:
        name_prefix = prep_circ.name + "_"
        circuits_this_stateprep = grouping_operator.construct_evaluation_circuit(
            wave_function=prep_circ,
            statevector_mode=False,
            use_simulator_snapshot_mode=False,
            circuit_name_prefix=name_prefix,
        )
        circuits_to_execute += circuits_this_stateprep
    return circuits_to_execute


def _compute_pauli_means_and_cov_for_one_basis(
    paulis: List[Pauli], counts: Dict[str, int]
) -> Tuple[NDArray, NDArray]:
    """Compute means and covariances for one Pauli basis.

    Args:
        - paulis: List of Paulis on which to infer means and covariances
        - counts: Dictionary containing the shot counts and metadata

    Returns:
        - Tuple[ndarray, ndarray]: Inferred means and coariances for Paulis in the group
    """
    means = np.array([_measure_pauli_z(counts, pauli) for pauli in paulis])
    cov = np.array(
        [
            [
                _covariance(counts, pauli_1, pauli_2, avg_1, avg_2)
                for pauli_2, avg_2 in zip(paulis, means)
            ]
            for pauli_1, avg_1 in zip(paulis, means)
        ]
    )
    return means, cov


def _measure_pauli_z(data: Dict[str, int], pauli: Pauli) -> float:
    """Measure expectation values for post-rotated Paulis in group.

    Args:
        - data: Dictionary containing the shot counts and metadata
        - pauli: a Pauli object

    Returns:
        - float: Expected value of paulis given data
    """
    observable = 0.0
    num_shots = sum(data.values())
    p_z_or_x = np.logical_or(pauli.z, pauli.x)
    for key, value in data.items():
        bitstr = np.asarray(list(key))[::-1].astype(int).astype(bool)
        # pylint: disable=no-member
        sign = -1.0 if np.logical_xor.reduce(np.logical_and(bitstr, p_z_or_x)) else 1.0
        observable += sign * value
    observable /= num_shots
    return observable


def _covariance(
    data: Dict[str, int], pauli_1: Pauli, pauli_2: Pauli, avg_1: float, avg_2: float
) -> float:
    """Compute the covariance matrix element between two post-rotated Paulis.

    Args:
        data: Dictionary containing the shot counts and metadata
        pauli_1: A Pauli class member
        pauli_2: A Pauli class member
        avg_1: Expectation value of pauli_1 on `data`
        avg_2: Expectation value of pauli_2 on `data`
    Returns:
        float: The element of the covariance matrix between two Paulis
    """
    cov = 0.0
    num_shots = sum(data.values())

    if num_shots == 1:
        return cov

    p1_z_or_x = np.logical_or(pauli_1.z, pauli_1.x)
    p2_z_or_x = np.logical_or(pauli_2.z, pauli_2.x)
    for key, value in data.items():
        bitstr = np.asarray(list(key))[::-1].astype(int).astype(bool)
        # pylint: disable=no-member
        sign_1 = (
            -1.0 if np.logical_xor.reduce(np.logical_and(bitstr, p1_z_or_x)) else 1.0
        )
        sign_2 = (
            -1.0 if np.logical_xor.reduce(np.logical_and(bitstr, p2_z_or_x)) else 1.0
        )
        cov += (sign_1 - avg_1) * (sign_2 - avg_2) * value
    cov /= num_shots - 1
    return cov


def _execute_with_retry(
    service: QiskitRuntimeService,
    inputs: Dict[str, Any],
    options: Dict[str, Any],
    session_id: Optional[str],
    start_session: bool,
) -> Tuple[SamplerResult, str]:
    """Execute an IBMQ job and automatically re-initiates the job if it fails.

    Args:
        - service: The Qiskit runtime service used to call the Sampler program
        - inputs: Inputs to the Sampler program
        - options: Backend options
        - session_id: The session ID to use when invoking the Sampler program
        - start_session: Whether to start a new session with this job
    Returns:
        - Tuple[SamplerResult, str]: Results from the sampler and the resulting job ID
    """
    result = None
    trials = 0
    ran_job_ok = False
    while not ran_job_ok:
        try:
            job = service.run(
                program_id="sampler",
                inputs=inputs,
                options=options,
                result_decoder=SamplerResultDecoder,
                session_id=session_id,
                start_session=start_session,
            )
            result = job.result()
            ran_job_ok = True
        except (IBMQJobFailureError, IBMQJobApiError, IBMQJobInvalidStateError) as err:
            print("Error running job, will retry in 5 mins.")
            print("Error:", err)
            # Wait 5 mins and try again. Hopefully this handles network outages etc,
            # and also if user cancels a (stuck) job through IQX.
            # Add more error types to the exception as new ones crop up (as appropriate).
            time.sleep(300)
            trials += 1
            if trials > 100:
                raise RuntimeError(
                    "Timed out trying to run job successfully (100 attempts)"
                )
    return result, job.job_id
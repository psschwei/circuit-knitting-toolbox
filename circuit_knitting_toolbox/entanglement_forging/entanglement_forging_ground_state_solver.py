# This code is a Qiskit project.

# (C) Copyright IBM 2022.

# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""File containing the entanglement forging ground state solver class and associated support methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import (
    Callable,
    Iterable,
    Sequence,
    Union,
)

import scipy
import numpy as np

from qiskit import QuantumCircuit
from qiskit.circuit import Instruction
from qiskit.algorithms.optimizers import SPSA, Optimizer, OptimizerResult
from qiskit.opflow import PauliSumOp, OperatorBase
from qiskit.quantum_info import Statevector
from qiskit.result import Result
from qiskit_nature import ListOrDictType
from qiskit_nature.algorithms import GroundStateSolver
from qiskit_nature.operators.second_quantization import SecondQuantizedOp
from qiskit_nature.problems.second_quantization import (
    BaseProblem,
    ElectronicStructureProblem,
)
from qiskit_nature.results import EigenstateResult
from qiskit_ibm_runtime import QiskitRuntimeService, Options

from .entanglement_forging_ansatz import EntanglementForgingAnsatz
from .entanglement_forging_knitter import EntanglementForgingKnitter
from .entanglement_forging_operator import EntanglementForgingOperator
from .cholesky_decomposition import cholesky_decomposition, convert_cholesky_operator

OBJECTIVE = Callable[[np.ndarray], float]
MINIMIZER = Callable[
    [
        OBJECTIVE,  # the objective function to minimize
        np.ndarray,  # the initial point for the optimization
    ],
    Union[scipy.optimize.OptimizeResult, OptimizerResult],
]


@dataclass
class EntanglementForgingEvaluation:
    """Entanglement Forging Evaluation."""

    parameters: Sequence[float]
    eigenvalue: float
    eigenstate: np.ndarray


@dataclass
class EntanglementForgingHistory:
    """Entanglement Forging History."""

    evaluations: list[EntanglementForgingEvaluation] = field(default_factory=list)
    optimal_evaluation: EntanglementForgingEvaluation | None = None

    def store_evaluation(self, evaluation: EntanglementForgingEvaluation):
        """Store an evaluation iteration and update optimal values, as necessary."""
        self.evaluations.append(evaluation)
        if (
            self.optimal_evaluation is None
            or evaluation.eigenvalue < self.optimal_evaluation.eigenvalue
        ):
            self.optimal_evaluation = evaluation


class EntanglementForgingResult(EigenstateResult):
    """Entanglement Forging Result."""

    def __init__(self) -> None:
        """Initialize `EigenstateResult` parent class and set class fields."""
        super().__init__()
        self._energy_shift: float = 0.0
        self._history: list[EntanglementForgingEvaluation] = []

    @property
    def groundenergy(self) -> float | None:
        """Return ground energy."""
        return self._groundenergy

    @groundenergy.setter
    def groundenergy(self, energy: float | None) -> None:
        """Set ground energy."""
        self._groundenergy = energy

    @property
    def history(self) -> list[EntanglementForgingEvaluation]:
        """Return optimizer history."""
        return self._history

    @history.setter
    def history(self, history: list[EntanglementForgingEvaluation]) -> None:
        """Set optimizer history."""
        self._history = history

    @property
    def groundstate(
        self,
    ) -> str | dict | Result | list | np.ndarray | Statevector | QuantumCircuit | Instruction | OperatorBase | None:
        """Return ground state."""
        return self._groundstate

    @groundstate.setter
    def groundstate(
        self,
        groundstate: str
        | dict
        | Result
        | list
        | np.ndarray
        | Statevector
        | QuantumCircuit
        | Instruction
        | OperatorBase
        | None,
    ) -> None:
        """Set ground state."""
        self._groundstate = groundstate

    @property
    def energy_shift(self) -> float:
        """Return the energy shift."""
        return self._energy_shift

    @energy_shift.setter
    def energy_shift(self, value: float) -> None:
        """Set the energy shift."""
        self._energy_shift = value

    @property
    def elapsed_time(self) -> float:
        """Return the elapsed time."""
        return self._elapsed_time

    @elapsed_time.setter
    def elapsed_time(self, value: float):
        """Set the elapsed time."""
        self._elapsed_time = value


class EntanglementForgingGroundStateSolver(GroundStateSolver):
    """A class which estimates the ground state energy of a molecule."""

    def __init__(
        self,
        ansatz: EntanglementForgingAnsatz | None = None,
        service: QiskitRuntimeService | None = None,
        optimizer: Optimizer | MINIMIZER | None = None,
        initial_point: np.ndarray | None = None,
        orbitals_to_reduce: Sequence[int] | None = None,
        backend_names: str | list[str] | None = None,
        options: Options | list[Options] | None = None,
    ):
        """
        Assign the necessary class variables and initialize any defaults.

        Args:
            - ansatz: Class which holes the ansatz circuit and bitstrings
            - service: The service used to spawn Qiskit primitives
            - optimizer: Optimizer to use to optimize the ansatz circuit parameters
            - initial_point: Initial values for ansatz parameters
            - orbitals_to_reduce: List of orbital indices to remove from the problem before
                decomposition.
            - backend_names: Backend name or list of backend names to use during parallel computation
            - options: Options or list of options to be applied to the backends

        Returns:
            - None
        """
        # These fields will be used when solve is called
        self._knitter: EntanglementForgingKnitter | None = None
        self._history: EntanglementForgingHistory = EntanglementForgingHistory()
        self._energy_shift = 0.0

        # Set class fields
        self._ansatz: EntanglementForgingAnsatz | None = ansatz
        self._service: QiskitRuntimeService | None = service
        self._initial_point: np.ndarray | None = initial_point
        self._orbitals_to_reduce = orbitals_to_reduce
        self.backend_names = backend_names  # type: ignore
        self.options = options

        self._optimizer: Optimizer | MINIMIZER = optimizer or SPSA()

    @property
    def ansatz(self) -> EntanglementForgingAnsatz | None:
        """Return ansatz."""
        return self._ansatz

    @ansatz.setter
    def ansatz(self, ansatz: EntanglementForgingAnsatz | None) -> None:
        """Set the ansatz."""
        self._ansatz = ansatz

    @property
    def service(self) -> QiskitRuntimeService | None:
        """Return service."""
        return self._service

    @service.setter
    def service(self, service: QiskitRuntimeService | None) -> None:
        """Set the service."""
        self._service = service

    @property
    def orbitals_to_reduce(self) -> Sequence[int] | None:
        """Return the orbitals to reduce."""
        return self._orbitals_to_reduce

    @orbitals_to_reduce.setter
    def orbitals_to_reduce(self, orbitals_to_reduce: Sequence[int] | None) -> None:
        """Set the orbitals to reduce."""
        self._orbitals_to_reduce = orbitals_to_reduce

    @property
    def optimizer(self) -> Optimizer:
        """Return the optimizer."""
        return self._optimizer

    @optimizer.setter
    def optimizer(self, optimizer: Optimizer) -> None:
        """Set the optimizer."""
        self._optimizer = optimizer

    @property
    def initial_point(self) -> np.ndarray | None:
        """Return the initial point."""
        return self._initial_point

    @initial_point.setter
    def initial_point(self, initial_point: np.ndarray | None) -> None:
        """Set the initial point."""
        self._initial_point = initial_point

    @property
    def backend_names(self) -> list[str] | None:
        """Return the backend names."""
        return self._backend_names

    @backend_names.setter
    def backend_names(self, backend_names: str | list[str]) -> None:
        """Set the backend names."""
        if isinstance(backend_names, str):
            self._backend_names = [backend_names]
        else:
            self._backend_names = backend_names

    @property
    def options(self) -> list[Options] | None:
        """Return the options."""
        return self._options

    @options.setter
    def options(self, options: Options | list[Options]) -> None:
        """Set the options."""
        if isinstance(options, Options):
            self._options = [options]
        else:
            self._options = options

    def solve(
        self,
        problem: BaseProblem,
        aux_operators: ListOrDictType[SecondQuantizedOp | PauliSumOp] | None = None,
    ) -> EigenstateResult:
        """Compute Ground State properties.

        Args:
            - problem: a class encoding a problem to be solved.
            - aux_operators: Additional auxiliary operators to evaluate.

        Returns:
            - An interpreted :class:`~.EigenstateResult`. For more information see also
            :meth:`~.BaseProblem.interpret`.
        """
        if not isinstance(problem, ElectronicStructureProblem):
            raise AttributeError(
                "EntanglementForgingGroundStateSolver only accepts ElectronicStructureProblem as input to its solve method."
            )
        if self._backend_names and self._options:
            if len(self._backend_names) != len(self._options):
                if len(self._options) == 1:
                    self._options = [self._options[0]] * len(self._backend_names)
                else:
                    raise AttributeError(
                        f"The list of backend names is length ({len(self._backend_names)}), but the list of options is length ({len(self._options)}). It is ambiguous how to combine the options with the backends."
                    )
        if self._ansatz is None:
            raise AttributeError("Ansatz must be set before calling solve.")
        if self._initial_point is None:
            self._initial_point = np.array(
                [0.0 for i in range(len(self._ansatz.circuit_u.parameters))]
            )

        hamiltonian_terms = self.get_qubit_operators(problem)
        ef_operator = convert_cholesky_operator(hamiltonian_terms, self._ansatz)

        if self._service is not None:
            backend_names = self._backend_names or ["ibmq_qasm_simulator"]
            self._knitter = EntanglementForgingKnitter(
                self._ansatz,
                service=self._service,
                backend_names=backend_names,
                options=self._options,
            )
        else:
            self._knitter = EntanglementForgingKnitter(
                self._ansatz, options=self._options
            )
        self._history = EntanglementForgingHistory()
        self._eval_count = 0

        evaluate_eigenvalue = self.get_eigenvalue_evaluation(ef_operator)

        start_time = time()

        if callable(self._optimizer):
            self.optimizer(fun=evaluate_eigenvalue, x0=self._initial_point)
        else:
            self.optimizer.minimize(fun=evaluate_eigenvalue, x0=self._initial_point)

        elapsed_time = time() - start_time

        optimal_evaluation = self._history.optimal_evaluation
        if optimal_evaluation is None:
            raise RuntimeError("Unable to retrieve optimal evaluation.")

        result = EntanglementForgingResult()
        result.eigenenergies = np.array(
            [e.eigenvalue for e in self._history.evaluations]
        )
        result.eigenstates = np.array([e.eigenstate for e in self._history.evaluations])  # type: ignore
        if self._history.optimal_evaluation is None:
            raise ValueError(
                "Something unexpected happened, and the solver was not able to find an optimal parametrization."
            )
        result.groundenergy = self._history.optimal_evaluation.eigenvalue
        result.groundstate = self._history.optimal_evaluation.eigenstate
        result.energy_shift = self._energy_shift
        result.history = self._history.evaluations
        result.elapsed_time = elapsed_time

        # Close any runtime sessions
        self._knitter.close_sessions()

        return result

    def get_eigenvalue_evaluation(
        self, operator: EntanglementForgingOperator
    ) -> Callable[[Sequence[float]], float | Iterable[float]]:
        """Produce a callable function which provides an estimation of the minimum eigenvalue of the input operator.

        Args:
          - operator (EntanglementForgingOperator): The decomposed Hamiltonian in entanglement forging format
        Returns:
          - callable (Callable):  Callable function which provides an estimation of the mihnimum eigenvalue
              of the input operator given some ansatz circuit parameters.
        """

        def evaluate_eigenvalue(parameters: Sequence[float]) -> float:
            if self._knitter is None:
                raise RuntimeError("Knitter must be set before evaluating eigenvalue.")

            eigenvalue, schmidt_coeffs, _ = self._knitter(
                ansatz_parameters=parameters, forged_operator=operator
            )
            self._history.store_evaluation(
                EntanglementForgingEvaluation(parameters, eigenvalue, schmidt_coeffs)
            )

            return eigenvalue

        return evaluate_eigenvalue

    def get_qubit_operators(
        self,
        problem: BaseProblem,
        aux_operators: ListOrDictType[SecondQuantizedOp | PauliSumOp] | None = None,
    ) -> tuple[PauliSumOp, ListOrDictType[PauliSumOp] | None]:
        """Construct decomposed qubit operators from an ``ElectronicStructureProblem``.

        Args:
          - problem (BaseProblem): A class encoding a problem to be solved.
          - aux_operators (ListOrDictType[SecondQuantizedOp | PauliSumOp]): Additional auxiliary operators to evaluate.

        Returns:
          - hamiltonian_ops: qubit operator representing the decomposed Hamiltonian.
        """
        if not isinstance(problem, ElectronicStructureProblem):
            raise TypeError(
                "EntanglementForgingGroundStateSolver only supports ElectronicStructureProblem."
            )
        hamiltonian_ops, self._energy_shift = cholesky_decomposition(
            problem, self._orbitals_to_reduce
        )
        return hamiltonian_ops

    def returns_groundstate(self) -> bool:
        """Whether this class returns only the ground state energy or also the ground state itself.

        Returns:
            - True, if this class also returns the ground state in the results object.
            False otherwise.
        """
        return True

    @property
    def qubit_converter(self):
        """Not implemented."""
        raise NotImplementedError

    @property
    def solver(self):
        """Not implemented."""
        raise NotImplementedError

    def evaluate_operators(
        self,
        state: str
        | dict
        | Result
        | list
        | np.ndarray
        | Statevector
        | QuantumCircuit
        | Instruction
        | OperatorBase,
        operators: PauliSumOp | OperatorBase | list | dict,
    ) -> float | list[float] | dict[str, list[float]]:
        """Not necessary to implement."""
        raise NotImplementedError(
            "This class method is not used by EntanglementForgingGroundStateSolver."
        )

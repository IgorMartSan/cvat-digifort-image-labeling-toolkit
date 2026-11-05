from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class BaseTrainer(ABC):
    """Interface base para treinadores de modelos de visão computacional."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nome identificador do framework (ex: yolo11, rtdetr, sam)."""
        pass

    @property
    @abstractmethod
    def supported_modes(self) -> List[str]:
        """Modalidades suportadas, ex: ['detection', 'segmentation']."""
        pass

    @abstractmethod
    def prepare_dataset(
        self,
        items: List[Dict[str, Any]],
        mode: str,
        label_names: Dict[int, str],
        output_dir: Path,
        seed: int = 42,
        include_empty: bool = False,
    ) -> Dict[str, Any]:
        """Formata e salva o dataset no disco de acordo com o padrão do framework."""
        pass

    @abstractmethod
    def train(
        self,
        dataset_info: Dict[str, Any],
        mode: str,
        epochs: int,
        batch: int,
        image_size: int,
        device: str,
        output_dir: Path,
        metadata: Dict[str, Any],
        weights: Optional[str] = None,
        model_name: Optional[str] = None,
        log_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Path:
        """Executa o treinamento do modelo e retorna o caminho do melhor checkpoint."""
        pass


class BasePredictor(ABC):
    """Interface base para inferência e anotação automática."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nome do framework."""
        pass

    @abstractmethod
    def load_model(self, checkpoint_path: Path) -> Any:
        """Carrega os pesos do modelo em memória."""
        pass

    @abstractmethod
    def predict_frame(
        self,
        model: Any,
        image_bytes: bytes,
        mode: str,
        confidence: float,
    ) -> List[Dict[str, Any]]:
        """
        Executa a predição para um único frame.
        Retorna lista de objetos detectados com formato normalizado:
        [
            {
                "class_index": int,
                "class_name": str,
                "confidence": float,
                "type": "rectangle" | "polygon",
                "points": list[float]  # Coordenadas em pixels para CVAT
            }
        ]
        """
        pass

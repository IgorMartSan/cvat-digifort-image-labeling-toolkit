from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.core.base_framework import BaseTrainer
from app.core.config import MODELS_DIR
from app.frameworks.yolo.dataset import build_yolo_dataset


class YoloTrainer(BaseTrainer):
    @property
    def name(self) -> str:
        return "yolo11"

    @property
    def supported_modes(self) -> List[str]:
        return ["detection", "segmentation"]

    def prepare_dataset(
        self,
        items: List[Dict[str, Any]],
        mode: str,
        label_names: Dict[int, str],
        output_dir: Path,
        seed: int = 42,
        include_empty: bool = False,
    ) -> Dict[str, Any]:
        yaml_path, counts = build_yolo_dataset(
            items, mode, label_names, output_dir, seed=seed, include_empty=include_empty
        )
        return {"yaml_path": yaml_path, "counts": counts}

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
        from ultralytics import YOLO

        yaml_path = dataset_info["yaml_path"]

        # Determina os pesos base (Nano, Small, Medium, Large ou custom)
        resolved_weights = weights
        if not resolved_weights:
            base_model = (model_name or "yolo11n").strip()
            if base_model.endswith(".pt"):
                base_name = base_model[:-3]
            else:
                base_name = base_model

            # Normaliza sufixo se for segmentação
            clean_base = base_name.replace("-seg", "")
            if mode == "segmentation":
                resolved_weights = f"{clean_base}-seg.pt"
            else:
                resolved_weights = f"{clean_base}.pt"

        if log_callback:
            log_callback({
                "type": "log",
                "text": f"Iniciando treinamento com pesos base '{resolved_weights}', {epochs} épocas, batch {batch}, imgsz {image_size}...",
            })

        model = YOLO(resolved_weights)

        # Registra callbacks do Ultralytics para capturar progresso das épocas
        def on_fit_epoch_end(trainer: Any) -> None:
            try:
                epoch = int(trainer.epoch) + 1
                total_epochs = int(trainer.epochs)
                metrics = trainer.metrics or {}
                # Obtém perdas
                loss_val = float(getattr(trainer, "loss", 0.0) or 0.0)
                # Formata log limpo
                map50 = metrics.get("metrics/mAP50(B)", metrics.get("metrics/mAP50(M)", 0.0))
                if log_callback:
                    log_callback({
                        "type": "epoch",
                        "epoch": epoch,
                        "total_epochs": total_epochs,
                        "percent": round((epoch / total_epochs) * 100, 1),
                        "loss": round(loss_val, 4),
                        "map50": round(float(map50), 4),
                        "text": f"Época {epoch}/{total_epochs} concluída - Loss: {loss_val:.4f} | mAP50: {float(map50):.4f}",
                    })
            except Exception:
                pass

        model.add_callback("on_fit_epoch_end", on_fit_epoch_end)

        result = model.train(
            data=str(yaml_path),
            epochs=epochs,
            batch=batch,
            imgsz=image_size,
            device=None if device == "auto" else device,
            project=str(output_dir),
            name="training",
            exist_ok=True,
            cache="disk",
            verbose=False,
        )

        best = Path(result.save_dir) / "weights" / "best.pt"
        if not best.exists():
            raise RuntimeError(f"Treinamento encerrou sem gerar 'best.pt': {best}")

        destination = output_dir / "best.pt"
        destination.write_bytes(best.read_bytes())

        metrics = {
            key: float(value)
            for key, value in getattr(result, "results_dict", {}).items()
            if isinstance(value, (int, float))
        }

        metadata.update({
            "framework": self.name,
            "mode": mode,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "epochs": epochs,
            "batch": batch,
            "image_size": image_size,
            "metrics": metrics,
            "ultralytics_version": __import__("ultralytics").__version__,
        })

        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if log_callback:
            log_callback({
                "type": "completed",
                "checkpoint": str(destination),
                "text": f"Treinamento concluído com sucesso! Modelo salvo em: {destination.name}",
            })

        return destination


def list_available_models() -> List[Dict[str, Any]]:
    """Varre models/ em busca de checkpoints com metadata.json."""
    found = []
    for metadata_path in MODELS_DIR.glob("*/run_*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            checkpoint = metadata_path.parent / "best.pt"
            if checkpoint.is_file():
                run_name = f"{metadata_path.parent.parent.name}/{metadata_path.parent.name}"
                found.append({
                    "id": run_name,
                    "checkpoint_path": str(checkpoint),
                    "run_dir": str(metadata_path.parent),
                    "mode": metadata.get("mode", "detection"),
                    "framework": metadata.get("framework", "yolo11"),
                    "trained_at": metadata.get("trained_at", ""),
                    "images_selected": metadata.get("images_selected", 0),
                    "epochs": metadata.get("epochs", 0),
                    "metrics": metadata.get("metrics", {}),
                    "label_ids": metadata.get("label_ids", {}),
                    "class_mapping": metadata.get("class_mapping", {}),
                    "project_name": metadata.get("project", {}).get("name", "N/A"),
                })
        except Exception:
            continue
    return sorted(found, key=lambda x: x["trained_at"], reverse=True)

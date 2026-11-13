from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import APP_ROOT, DATA_DIR, MODELS_DIR
from app.core.cvat import get_cvat_client, get_project_labels, list_projects
from app.core.events import sse_manager
from app.frameworks import TRAINERS
from app.frameworks.yolo.annotations import analyze_annotations, shapes_from
from app.frameworks.yolo.dataset import collect_task_items, select_stratified
from app.modules.training.schemas import StartTrainRequest

logger = logging.getLogger("cvat_toolkit.training")

current_train_status: Dict[str, Any] = {
    "is_running": False,
    "current_epoch": 0,
    "total_epochs": 0,
    "percent": 0.0,
    "last_loss": 0.0,
    "last_map50": 0.0,
    "error": None,
    "checkpoint": None,
}


def analyze_dataset_service(project_id: int, task_ids: List[int]) -> Dict[str, Any]:
    with get_cvat_client() as client:
        project = client.projects.retrieve(project_id)
        project_labels = get_project_labels(project_id)
        valid_tasks = [t for t in project.get_tasks() if int(t.id) in task_ids]

        all_shapes = []
        total_images = 0
        annotated_frames_detection = 0
        annotated_frames_segmentation = 0

        for task in valid_tasks:
            meta = task.get_meta()
            size = getattr(meta, "size", 0)
            deleted_frames = set(getattr(meta, "deleted_frames", []) or [])
            if isinstance(meta, dict):
                size = meta.get("size", 0)
                deleted_frames = set(meta.get("deleted_frames", []) or [])

            active_images = max(0, int(size) - len(deleted_frames))
            total_images += active_images

            annotations = task.get_annotations()
            task_shapes = shapes_from(annotations)
            # Ignora anotações de frames deletados no CVAT
            valid_task_shapes = [
                s for s in task_shapes
                if int(getattr(s, "frame", 0) if hasattr(s, "frame") else s.get("frame", 0)) not in deleted_frames
            ]
            all_shapes.extend(valid_task_shapes)

            frame_types: Dict[int, set] = {}
            for shape in valid_task_shapes:
                frame = int(getattr(shape, "frame", 0) if hasattr(shape, "frame") else shape.get("frame", 0))
                stype = str(getattr(shape, "type", "") if hasattr(shape, "type") else shape.get("type", "")).lower()
                frame_types.setdefault(frame, set()).add(stype)

            annotated_frames_detection += sum(bool(kinds & {"rectangle", "polygon"}) for kinds in frame_types.values())
            annotated_frames_segmentation += sum("polygon" in kinds for kinds in frame_types.values())

        stats = analyze_annotations({"shapes": all_shapes}, total_images, project_labels)

        return {
            "total_images": total_images,
            "annotated_images": stats.annotated_images,
            "empty_images": stats.empty_images,
            "rectangles": stats.rectangles,
            "polygons": stats.polygons,
            "labels": stats.labels,
            "incompatible": stats.incompatible,
            "detection_compatible_frames": annotated_frames_detection,
            "segmentation_compatible_frames": annotated_frames_segmentation,
        }


def execute_training_job_sync(job: Any, queue_mgr: Any) -> None:
    """Executado pelo worker único da fila de tarefas."""
    global current_train_status
    req = StartTrainRequest(**job.payload)

    current_train_status.update({
        "is_running": True,
        "current_epoch": 0,
        "total_epochs": req.epochs,
        "percent": 0.0,
        "last_loss": 0.0,
        "last_map50": 0.0,
        "error": None,
        "checkpoint": None,
    })

    sse_manager.clear_history("training")
    queue_mgr.update_job_progress(job.id, 5.0, "Conectando ao CVAT e recuperando tarefas...")
    queue_mgr.append_log(
        job.id,
        f"🚀 Inicializando processo de treinamento (Framework: {req.framework}, Modelo: {req.model_name}, Modo: {req.mode})...",
    )

    trainer = TRAINERS.get(req.framework)
    if not trainer:
        raise ValueError(f"Framework '{req.framework}' não suportado.")

    run_dataset_dir: Optional[Path] = None
    run_output_dir: Optional[Path] = None

    try:
        with get_cvat_client() as client:
            project = client.projects.retrieve(req.project_id)
            project_labels = get_project_labels(req.project_id)
            tasks = [t for t in project.get_tasks() if int(t.id) in req.task_ids]

            queue_mgr.append_log(
                job.id,
                f"📥 Coletando imagens das tarefas selecionadas: {', '.join(map(str, req.task_ids))}...",
            )

            items = []
            total_tasks = len(tasks)
            for task_idx, t in enumerate(tasks):
                if queue_mgr.is_cancel_requested(job.id):
                    raise InterruptedError("Treinamento cancelado pelo usuário.")

                def on_download_progress(curr: int, total: int, text: str) -> None:
                    step_pct = 5.0 + ((task_idx + (curr / max(total, 1))) / max(total_tasks, 1)) * 25.0
                    queue_mgr.update_job_progress(job.id, step_pct, text)

                task_items = collect_task_items(
                    t,
                    req.mode,
                    derive_polygons=req.derive_polygons,
                    include_empty=req.train_all_images,
                    on_progress=on_download_progress,
                )
                items.extend(task_items)
                queue_mgr.append_log(
                    job.id,
                    f"✓ Tarefa #{t.id} ({t.name}): {len(task_items)} frames carregados (modo imagens completas: {req.train_all_images}).",
                )

            if len(items) < 2:
                raise ValueError("São necessárias pelo menos 2 imagens com anotações válidas para treinar.")

            if req.sample_limit and 2 <= req.sample_limit < len(items):
                queue_mgr.append_log(job.id, f"Aplicando amostragem estratificada para {req.sample_limit} imagens...")
                items = select_stratified(items, req.sample_limit, seed=42)

            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_dataset_dir = DATA_DIR / f"dataset_{now_str}"

            queue_mgr.update_job_progress(job.id, 35.0, f"Gravando dataset YOLO em {run_dataset_dir.name}...")
            queue_mgr.append_log(job.id, f"Formatando e gravando dataset YOLO em {run_dataset_dir.name}...")

            dataset_info = trainer.prepare_dataset(
                items=items,
                mode=req.mode,
                label_names=project_labels,
                output_dir=run_dataset_dir,
                seed=42,
                include_empty=req.train_all_images,
            )

            # Prepara pasta de saída do modelo
            mode_dir = MODELS_DIR / req.mode
            mode_dir.mkdir(parents=True, exist_ok=True)
            existing_runs = [int(p.name[4:]) for p in mode_dir.glob("run_*") if p.name[4:].isdigit()]
            next_run_num = max(existing_runs, default=0) + 1
            run_output_dir = mode_dir / f"run_{next_run_num:03d}"
            run_output_dir.mkdir(parents=True, exist_ok=True)

            metadata = {
                "project": {"id": int(project.id), "name": project.name},
                "task_ids": req.task_ids,
                "label_ids": project_labels,
                "images_selected": len(items),
                "model_name": req.model_name,
                "train_all_images": req.train_all_images,
                "dataset_dir": str(run_dataset_dir),
                "derived_boxes_from_polygons": req.derive_polygons,
            }

            def on_log_event(event: Dict[str, Any]) -> None:
                if queue_mgr.is_cancel_requested(job.id):
                    raise InterruptedError("Treinamento cancelado pelo usuário.")

                if event.get("type") == "epoch":
                    epoch = int(event.get("epoch", 0))
                    total_epochs = int(event.get("total_epochs", req.epochs))
                    pct = 35.0 + (epoch / max(total_epochs, 1)) * 63.0
                    loss_v = event.get("loss", 0.0)
                    map_v = event.get("map50", 0.0)

                    current_train_status["current_epoch"] = epoch
                    current_train_status["percent"] = round(pct, 1)
                    current_train_status["last_loss"] = loss_v
                    current_train_status["last_map50"] = map_v

                    step_text = f"Treinando: Época {epoch}/{total_epochs} (Loss: {loss_v} | mAP50: {map_v})"
                    queue_mgr.update_job_progress(job.id, pct, step_text, metrics=event)

                if event.get("text"):
                    queue_mgr.append_log(job.id, event["text"])
                sse_manager.broadcast("training", event)

            checkpoint = trainer.train(
                dataset_info=dataset_info,
                mode=req.mode,
                epochs=req.epochs,
                batch=req.batch,
                image_size=req.image_size,
                device=req.device,
                output_dir=run_output_dir,
                metadata=metadata,
                weights=req.custom_weights,
                model_name=req.model_name,
                log_callback=on_log_event,
            )

            current_train_status["checkpoint"] = str(checkpoint)
            current_train_status["is_running"] = False

            job.metrics["checkpoint"] = str(checkpoint)
            job.metrics["run_dir"] = str(run_output_dir)
            job.metrics["best_model"] = f"{run_output_dir.name}/best.pt"

            queue_mgr.append_log(
                job.id,
                f"🎉 Treinamento finalizado com sucesso! Modelo salvo em: {run_output_dir.name}/best.pt",
                log_type="success",
            )
            sse_manager.broadcast("training", {
                "type": "success",
                "text": f"🎉 Treinamento finalizado com sucesso! Modelo salvo em: {run_output_dir.name}/best.pt",
                "checkpoint": str(checkpoint),
            })

    finally:
        # Limpeza obrigatória de imagens e arquivos temporários ao finalizar:
        if run_dataset_dir and run_dataset_dir.exists():
            try:
                shutil.rmtree(run_dataset_dir, ignore_errors=True)
                queue_mgr.append_log(job.id, f"🧹 Limpeza automática: dataset temporário '{run_dataset_dir.name}' removido.")
            except Exception as e:
                logger.warning(f"Erro ao remover dataset temporário {run_dataset_dir}: {e}")

        for tid in req.task_ids:
            task_cache = DATA_DIR / "cache" / f"task_{int(tid)}"
            if task_cache.exists():
                try:
                    shutil.rmtree(task_cache, ignore_errors=True)
                except Exception:
                    pass

        if run_output_dir and run_output_dir.exists():
            best_file = run_output_dir / "best.pt"
            if not best_file.exists():
                try:
                    shutil.rmtree(run_output_dir, ignore_errors=True)
                except Exception:
                    pass
            else:
                training_dir = run_output_dir / "training"
                if training_dir.exists():
                    for f in training_dir.glob("*.jpg"):
                        try:
                            f.unlink(missing_ok=True)
                        except Exception:
                            pass
                    shutil.rmtree(training_dir / "weights", ignore_errors=True)


def list_trained_models_service() -> List[Dict[str, Any]]:
    """Varre MODELS_DIR e retorna todos os modelos treinados com metadados detalhados."""
    models_list = []
    if not MODELS_DIR.is_dir():
        return models_list

    for mode_dir in sorted(MODELS_DIR.iterdir()):
        if not mode_dir.is_dir():
            continue
        mode = mode_dir.name

        for run_dir in sorted(mode_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            run_name = run_dir.name
            metadata_file = run_dir / "metadata.json"
            best_file = run_dir / "best.pt"

            training_best = run_dir / "training" / "weights" / "best.pt"
            if not best_file.exists() and training_best.exists():
                try:
                    best_file.write_bytes(training_best.read_bytes())
                except Exception:
                    pass

            metadata = {}
            if metadata_file.is_file():
                try:
                    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                except Exception:
                    metadata = {}

            size_bytes = best_file.stat().st_size if best_file.is_file() else 0
            size_mb = round(size_bytes / (1024 * 1024), 2)

            trained_at = metadata.get("trained_at", "")
            if not trained_at and run_dir.exists():
                mtime = run_dir.stat().st_mtime
                trained_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

            trained_at_formatted = "-"
            if trained_at:
                try:
                    cleaned = trained_at.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(cleaned)
                    trained_at_formatted = dt.strftime("%d/%m/%Y %H:%M:%S")
                except Exception:
                    trained_at_formatted = trained_at[:19].replace("T", " ")

            framework = metadata.get("framework", "yolo11")
            model_base = metadata.get("model_name", "YOLO")
            project_info = metadata.get("project", {})
            project_name = project_info.get("name", f"Projeto #{project_info.get('id', '-')}" if project_info.get("id") else "Manual/Importado")

            label_names = []
            if metadata.get("label_ids"):
                label_names = list(metadata["label_ids"].values())

            metrics = metadata.get("metrics", {})
            map50 = metrics.get("metrics/mAP50(B)") or metrics.get("map50", "-")
            if isinstance(map50, float):
                map50 = round(map50, 4)

            models_list.append({
                "id": f"{mode}/{run_name}",
                "mode": mode,
                "mode_label": "Detecção" if mode == "detection" else ("Segmentação" if mode == "segmentation" else mode.capitalize()),
                "run_name": run_name,
                "framework": framework,
                "model_base": model_base,
                "checkpoint_path": str(best_file) if best_file.is_file() else None,
                "has_weights": best_file.is_file(),
                "size_mb": size_mb,
                "run_dir": str(run_dir),
                "trained_at": trained_at,
                "trained_at_formatted": trained_at_formatted,
                "epochs": metadata.get("epochs", "-"),
                "batch": metadata.get("batch", "-"),
                "image_size": metadata.get("image_size", "-"),
                "metrics": metrics,
                "map50": map50,
                "project_name": project_name,
                "task_ids": metadata.get("task_ids", []),
                "labels": label_names,
            })

    models_list.sort(key=lambda m: m["trained_at"], reverse=True)
    return models_list


def delete_trained_model_service(mode: str, run_name: str) -> bool:
    """Exclui a pasta completa de um modelo treinado."""
    safe_mode = Path(mode).name
    safe_run = Path(run_name).name

    target_dir = MODELS_DIR / safe_mode / safe_run
    if target_dir.is_dir() and target_dir.resolve().is_relative_to(MODELS_DIR.resolve()):
        shutil.rmtree(target_dir, ignore_errors=True)
        return True
    return False


def get_model_details_service(mode: str, run_name: str) -> Optional[Dict[str, Any]]:
    """Lê todas as informações e arquivos da pasta de treino do YOLO tal qual a pasta do mesmo."""
    import csv
    import yaml

    safe_mode = Path(mode).name
    safe_run = Path(run_name).name
    run_dir = MODELS_DIR / safe_mode / safe_run
    if not run_dir.is_dir() or not run_dir.resolve().is_relative_to(MODELS_DIR.resolve()):
        return None

    metadata_file = run_dir / "metadata.json"
    metadata: Dict[str, Any] = {}
    if metadata_file.is_file():
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}

    training_dir = run_dir / "training"

    # Pesos do modelo (best.pt)
    best_file = run_dir / "best.pt"
    if not best_file.is_file() and (training_dir / "weights" / "best.pt").is_file():
        try:
            best_file.write_bytes((training_dir / "weights" / "best.pt").read_bytes())
        except Exception:
            pass

    has_weights = best_file.is_file()
    weights_size_mb = round(best_file.stat().st_size / (1024 * 1024), 2) if has_weights else 0

    # Hiperparâmetros do YOLO (args.yaml)
    args_yaml_file = training_dir / "args.yaml"
    args_data: Dict[str, Any] = {}
    if args_yaml_file.is_file():
        try:
            args_data = yaml.safe_load(args_yaml_file.read_text(encoding="utf-8")) or {}
        except Exception:
            try:
                for line in args_yaml_file.read_text(encoding="utf-8").splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        args_data[k.strip()] = v.strip()
            except Exception:
                pass

    # Histórico de épocas (results.csv)
    csv_file = training_dir / "results.csv"
    csv_rows: List[Dict[str, Any]] = []
    best_map50 = 0.0
    best_epoch = 0
    final_metrics: Dict[str, Any] = {}

    if csv_file.is_file():
        try:
            with open(csv_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cleaned_row: Dict[str, Any] = {}
                    for k, v in row.items():
                        if not k:
                            continue
                        clean_k = k.strip()
                        clean_v = v.strip()
                        try:
                            fval = float(clean_v)
                            cleaned_row[clean_k] = round(fval, 4) if not fval.is_integer() else int(fval)
                        except ValueError:
                            cleaned_row[clean_k] = clean_v
                    csv_rows.append(cleaned_row)

                    for mkey in ("metrics/mAP50(B)", "metrics/mAP50(M)", "map50"):
                        if mkey in cleaned_row:
                            try:
                                val = float(cleaned_row[mkey])
                                if val > best_map50:
                                    best_map50 = val
                                    best_epoch = int(cleaned_row.get("epoch", len(csv_rows)))
                            except (ValueError, TypeError):
                                pass

            if csv_rows:
                final_metrics = csv_rows[-1]
        except Exception as e:
            logger.warning(f"Erro ao ler results.csv em {csv_file}: {e}")

    # Coleta de imagens, gráficos e arquivos da pasta de treino
    plots = []
    labels_images = []
    validation_images = []
    training_batches = []
    other_images = []
    folder_files = []

    # Arquivos na raiz do modelo
    for root_item in sorted(run_dir.iterdir()):
        if root_item.is_file():
            sz = root_item.stat().st_size
            sz_str = f"{round(sz / (1024 * 1024), 2)} MB" if sz >= 1024 * 1024 else f"{round(sz / 1024, 1)} KB"
            folder_files.append({
                "name": root_item.name,
                "rel_path": root_item.name,
                "size_str": sz_str,
                "is_weights": root_item.suffix.lower() == ".pt",
                "is_json": root_item.suffix.lower() == ".json",
                "is_image": root_item.suffix.lower() in {".png", ".jpg", ".jpeg"},
                "url": f"/model_files/{safe_mode}/{safe_run}/{root_item.name}",
            })

    # Arquivos dentro de training/
    if training_dir.is_dir():
        for item in sorted(training_dir.iterdir()):
            if item.is_file():
                sz = item.stat().st_size
                sz_str = f"{round(sz / (1024 * 1024), 2)} MB" if sz >= 1024 * 1024 else f"{round(sz / 1024, 1)} KB"
                rel_url = f"/model_files/{safe_mode}/{safe_run}/training/{item.name}"
                ext = item.suffix.lower()
                is_img = ext in {".png", ".jpg", ".jpeg"}

                folder_files.append({
                    "name": f"training/{item.name}",
                    "rel_path": f"training/{item.name}",
                    "size_str": sz_str,
                    "is_image": is_img,
                    "is_csv": ext == ".csv",
                    "is_yaml": ext in {".yaml", ".yml"},
                    "url": rel_url,
                })

                if is_img:
                    name_lower = item.name.lower()
                    img_entry = {
                        "name": item.name,
                        "title": item.stem.replace("_", " ").title(),
                        "url": rel_url,
                        "size_str": sz_str,
                    }
                    if name_lower in {
                        "results.png", "confusion_matrix.png", "confusion_matrix_normalized.png",
                        "boxpr_curve.png", "boxf1_curve.png", "boxp_curve.png", "boxr_curve.png",
                        "maskpr_curve.png", "maskf1_curve.png", "maskp_curve.png", "maskr_curve.png",
                    }:
                        plots.append(img_entry)
                    elif "label" in name_lower and "batch" not in name_lower:
                        labels_images.append(img_entry)
                    elif "val_batch" in name_lower:
                        validation_images.append(img_entry)
                    elif "train_batch" in name_lower:
                        training_batches.append(img_entry)
                    else:
                        other_images.append(img_entry)

    def plot_sort_key(p):
        n = p["name"].lower()
        if n == "results.png":
            return (0, n)
        if "confusion_matrix" in n:
            return (1, n)
        if "boxpr" in n or "maskpr" in n:
            return (2, n)
        if "boxf1" in n or "maskf1" in n:
            return (3, n)
        return (4, n)

    plots.sort(key=plot_sort_key)

    trained_at = metadata.get("trained_at", "")
    if not trained_at and run_dir.exists():
        trained_at = datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc).isoformat()

    trained_at_formatted = "-"
    if trained_at:
        try:
            cleaned = trained_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            trained_at_formatted = dt.strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            trained_at_formatted = trained_at[:19].replace("T", " ")

    project_info = metadata.get("project", {})
    project_name = project_info.get("name", f"Projeto #{project_info.get('id', '-')}" if project_info.get("id") else "Manual/Importado")
    label_names = list(metadata.get("label_ids", {}).values()) if metadata.get("label_ids") else []

    return {
        "mode": safe_mode,
        "mode_label": "Detecção" if safe_mode == "detection" else ("Segmentação" if safe_mode == "segmentation" else safe_mode.capitalize()),
        "run_name": safe_run,
        "id": f"{safe_mode}/{safe_run}",
        "framework": metadata.get("framework", "yolo11"),
        "model_base": metadata.get("model_name", args_data.get("model", "yolo11")),
        "trained_at_formatted": trained_at_formatted,
        "project_name": project_name,
        "task_ids": metadata.get("task_ids", []),
        "labels": label_names,
        "has_weights": has_weights,
        "weights_size_mb": weights_size_mb,
        "weights_url": f"/model_files/{safe_mode}/{safe_run}/best.pt" if has_weights else None,
        "args": args_data,
        "epochs": metadata.get("epochs", args_data.get("epochs", len(csv_rows))),
        "batch": metadata.get("batch", args_data.get("batch", "-")),
        "image_size": metadata.get("image_size", args_data.get("imgsz", "-")),
        "best_map50": round(best_map50, 4) if best_map50 > 0 else (metadata.get("metrics", {}).get("metrics/mAP50(B)", "-")),
        "best_epoch": best_epoch,
        "total_epochs_completed": len(csv_rows),
        "final_metrics": final_metrics,
        "plots": plots,
        "labels_images": labels_images,
        "validation_images": validation_images,
        "training_batches": training_batches,
        "other_images": other_images,
        "folder_files": folder_files,
        "csv_rows": csv_rows,
        "csv_columns": list(csv_rows[0].keys()) if csv_rows else [],
    }


def start_training_job(req: StartTrainRequest) -> Any:
    """Insere o treinamento na fila única global e retorna o job criado."""
    from app.core.job_queue import job_queue

    title = f"Treinamento YOLO: Projeto #{req.project_id} ({req.model_name})"
    job = job_queue.enqueue(
        job_type="training",
        title=title,
        payload=req.model_dump(),
    )
    return job

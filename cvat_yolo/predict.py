from __future__ import annotations

import json
import sys
from pathlib import Path

from src.config import Config, ensure_dirs
from src.cvat_client import connect, project_tasks
from src.predictor import log_prediction, predict_task, resolve_label_mapping
from src.yolo import available_models


def choose(items, title: str):
    if not items:
        raise RuntimeError("Nenhuma opção disponível.")
    print(title)
    for i, item in enumerate(items, 1):
        if hasattr(item, "id") and hasattr(item, "name"):
            display = f"{item.name} — ID {item.id}"
        elif isinstance(item, tuple) and len(item) == 3:
            checkpoint, metadata, names = item
            display = f"YOLO {str(metadata.get('mode', 'detection')).title()}\n    Modelo: {checkpoint.parent.name}\n    Classes: {', '.join(names)}\n    Treinado com: {metadata.get('images_selected', '?')} imagens"
        else:
            display = str(item)
        print(f"[{i}] {display}")
    while True:
        value = input("Escolha: ").strip()
        if value.isdigit() and 1 <= int(value) <= len(items):
            return items[int(value) - 1]


def select_many(tasks):
    print("ID    Nome")
    for task in tasks:
        print(f"{task.id:<5} {task.name}")
    valid = {int(task.id) for task in tasks}
    while True:
        try:
            ids = list(dict.fromkeys(int(value.strip()) for value in input("IDs de tarefas (vírgula): ").split(",") if value.strip()))
        except ValueError:
            ids = []
        if ids and set(ids) <= valid:
            return [task for task in tasks if int(task.id) in ids]
        print("IDs inválidos.")


def main() -> int:
    print("========= CVAT YOLO PREDICTION =========")
    config = Config.from_env()
    ensure_dirs()
    try:
        models_available = available_models()
        if not models_available:
            raise RuntimeError("Nenhum checkpoint com metadata.json foi encontrado em models/.")
        with connect(config) as client:
            projects = list(client.projects.list())
            project = choose(projects, "Selecione o projeto:")
            tasks = project_tasks(project)
            selected_tasks = select_many(tasks)
            candidates = []
            for checkpoint, metadata in models_available:
                labels = metadata.get("label_ids", {})
                names = [labels.get(str(k), str(v)) for k, v in sorted(((int(k), v) for k, v in labels.items()))]
                candidates.append((checkpoint, metadata, names))
            selected = choose(candidates, "\n========= MODELOS DISPONÍVEIS =========")
            checkpoint, metadata, _ = selected
            if not checkpoint.is_file():
                raise RuntimeError(f"Checkpoint inexistente: {checkpoint}")
            from ultralytics import YOLO
            model = YOLO(str(checkpoint))
            names = model.names
            source_names = {int(index): str(name) for index, name in names.items()}
            destination_labels = list(project.get_labels())
            class_map = resolve_label_mapping(source_names, destination_labels, choose)
            print(f"\nProjeto: {project.name}\nTarefas: {', '.join(str(t.id) for t in selected_tasks)}")
            print(f"Modelo: {checkpoint.parent.parent.name}/{checkpoint.parent.name}")
            print("Mapeamento: " + ", ".join(f"{source_names[k]} → {next(x.name for x in destination_labels if int(x.id)==v)}" for k,v in class_map.items()))
            print("Frames com anotações existentes serão preservados e ignorados.")
            overwrite_auto = False
            if config.overwrite:
                print("Há suporte para substituir apenas predições anteriores marcadas como automáticas; anotações manuais continuam protegidas.")
                overwrite_auto = input("Confirmar substituição das predições automáticas existentes? (s/n): ").strip().lower() == "s"
            if input("Confirmar processamento e envio ao CVAT? (s/n): ").strip().lower() != "s":
                return 0
            mode = str(metadata.get("mode", "detection"))
            total = {key: 0 for key in ("processed", "objects", "sent", "failed", "skipped")}
            for task in selected_tasks:
                print(f"Processando task {task.id}: {task.name}")
                try:
                    counts = predict_task(task, model, mode, class_map, config.confidence, overwrite_auto=overwrite_auto)
                except Exception as exc:
                    print(f"Falha na task {task.id}: {exc}")
                    counts = {key: 0 for key in total}
                    counts["failed"] = 1
                for key in total:
                    total[key] += counts[key]
                print(f"  processados={counts['processed']}, objetos={counts['objects']}, enviados={counts['sent']}, ignorados={counts['skipped']}, falhas={counts['failed']}")
            log_prediction([int(t.id) for t in selected_tasks], str(checkpoint.relative_to(checkpoint.parents[1])), total)
            print("\nResultado final:")
            print(f"Frames processados: {total['processed']} | Objetos: {total['objects']} | Anotações enviadas: {total['sent']} | Ignorados: {total['skipped']} | Falhas: {total['failed']}")
        return 0
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

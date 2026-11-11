from __future__ import annotations

import sys
from importlib.metadata import version

from src.annotations import _value, analyze_annotations, shapes_from
from src.config import Config, ROOT, ensure_dirs
from src.cvat_client import connect, labels_by_id, project_tasks, task_is_completed
from src.dataset import collect_task
from src.yolo import _split, build_dataset, select_stratified, train_model


def choose(items, title: str):
    if not items:
        raise RuntimeError("Nenhuma opção disponível.")
    print(title)
    for i, item in enumerate(items, 1):
        if hasattr(item, "id") and hasattr(item, "name"):
            display = f"{item.name} — ID {item.id}"
        else:
            display = str(item)
        print(f"[{i}] {display}")
    while True:
        value = input("Escolha: ").strip()
        if value.isdigit() and 1 <= int(value) <= len(items):
            return items[int(value) - 1]


def main() -> int:
    print("========= CVAT YOLO TRAINING =========")
    config = Config.from_env()
    ensure_dirs()
    try:
        with connect(config) as client:
            projects = list(client.projects.list())
            project = choose(projects, "Selecione o projeto:")
            tasks = project_tasks(project)
            eligible = []
            print(f"\nProjeto: {project.name}\nID    Nome                         Estado jobs    Imagens")
            for task in tasks:
                done, state = task_is_completed(task)
                meta = task.get_meta()
                count = int(_value(meta, "size", 0))
                print(f"{task.id:<5} {str(task.name)[:28]:<28} {state:<13} {count}")
                if done:
                    eligible.append(task)
            if not eligible:
                raise RuntimeError("O projeto não tem tarefas cujos jobs estejam todos concluídos.")
            valid_ids = {int(task.id) for task in eligible}
            while True:
                raw = input("IDs de tarefas finalizadas para treinamento (separados por vírgula): ")
                try:
                    ids = list(dict.fromkeys(int(token.strip()) for token in raw.split(",") if token.strip()))
                except ValueError:
                    print("Informe IDs numéricos separados por vírgula.")
                    continue
                if ids and set(ids) <= valid_ids:
                    break
                print("Selecione somente IDs finalizados exibidos acima.")
            selected = [task for task in eligible if int(task.id) in ids]
            project_labels = labels_by_id(list(project.get_labels()))
            all_shapes, total, annotated_images = [], 0, 0
            detection_frames, segmentation_frames = 0, 0
            for task in selected:
                annotations = task.get_annotations()
                task_shapes = shapes_from(annotations)
                all_shapes.extend(task_shapes)
                total += int(_value(task.get_meta(), "size", 0))
                frame_types: dict[int, set[str]] = {}
                for shape in task_shapes:
                    frame_types.setdefault(int(_value(shape, "frame", -1)), set()).add(str(_value(shape, "type", "")).lower())
                annotated_images += len(frame_types)
                detection_frames += sum(bool(kinds & {"rectangle", "polygon"}) for kinds in frame_types.values())
                segmentation_frames += sum("polygon" in kinds for kinds in frame_types.values())
            # Aggregate using the project's labels, keyed by global frame offsets only for stats.
            stats = analyze_annotations({"shapes": all_shapes}, total, project_labels)
            stats.annotated_images = annotated_images
            stats.empty_images = max(0, total - annotated_images)
            print("\n========= ANÁLISE DO DATASET =========")
            print(f"Projeto: {project.name}\nTarefas: {', '.join(map(str, ids))}")
            print(f"Imagens totais: {total}\nImagens com anotações: {stats.annotated_images}\nImagens sem anotações: {stats.empty_images}")
            print(f"Retângulos: {stats.rectangles}\nPolígonos: {stats.polygons}")
            for name, count in sorted(stats.labels.items()):
                print(f"  {name}: {count}")
            if stats.incompatible:
                print("Anotações incompatíveis detectadas:")
                for kind, count in stats.incompatible.items():
                    print(f"  {kind}: {count}")
                print("Esses objetos não serão exportados.")
                if input("Continuar ignorando essas anotações? (s/n): ").strip().lower() != "s":
                    return 0
            print("\n========= TIPO DE TREINAMENTO =========\n[1] Detection\n[2] Segmentation\n[0] Cancelar")
            choice = input("Escolha: ").strip()
            if choice == "0":
                return 0
            if choice not in {"1", "2"}:
                print("Escolha inválida.")
                return 1
            mode = "detection" if choice == "1" else "segmentation"
            dropped = stats.polygons if mode == "detection" else stats.rectangles
            derived = mode == "detection" and stats.polygons > 0
            if mode == "detection" and stats.polygons:
                print("Detection pode derivar bounding boxes das coordenadas dos polígonos.")
                derived = input("Usar também bounding boxes derivados dos polígonos? (s/n): ").strip().lower() == "s"
                if not derived:
                    dropped = stats.polygons
            elif mode == "segmentation" and stats.rectangles:
                dropped = stats.rectangles
            if dropped:
                print(f"{dropped} demarcações incompatíveis com esta escolha serão ignoradas.")
                if input("Deseja continuar? (s/n): ").strip().lower() != "s":
                    return 0
            compatible_count = detection_frames if mode == "detection" else segmentation_frames
            print(f"\nImagens anotadas compatíveis disponíveis: {compatible_count}")
            print("[1] Todas as imagens anotadas compatíveis")
            print("[2] Todas as imagens, incluindo as sem anotação")
            print("[3] Definir quantidade somente de imagens anotadas")
            print("[4] Definir quantidade incluindo imagens sem anotação")
            count_choice = input("Escolha: ").strip()
            if count_choice not in {"1", "2", "3", "4"}:
                return 1
            include_empty = count_choice in {"2", "4"}
            available_count = total if include_empty else compatible_count
            print(f"Imagens disponíveis para seleção: {available_count}")
            limit = available_count
            if count_choice in {"3", "4"}:
                limit = int(input("Quantidade: ").strip())
                if not 2 <= limit <= available_count:
                    raise ValueError("A quantidade deve ser entre 2 e as imagens disponíveis.")
            items = []
            for task in selected:
                items.extend(collect_task(task, mode, derive_polygons=derived, include_empty=include_empty))
            items = select_stratified(items, min(limit, len(items)), seed=42)
            train_preview, val_preview = _split(items, seed=42)
            class_count = len({obj["label_id"] for item in items for obj in item["objects"]})
            print("\n========= RESUMO TREINAMENTO =========")
            print(f"Projeto: {project.name}\nTarefas: {', '.join(map(str, ids))}")
            print(f"Modalidade: {mode.title()}\nImagens selecionadas: {len(items)}")
            print(f"Treinamento: {len(train_preview)}\nValidação: {len(val_preview)}")
            print(f"Classes: {class_count}\nÉpocas: {config.epochs}\nBatch: {config.batch}\nResolução: {config.image_size}")
            if input("Iniciar treinamento? (s/n): ").strip().lower() != "s":
                return 0
            run_dataset = ROOT / "data" / f"dataset_{__import__('datetime').datetime.now():%Y%m%d_%H%M%S_%f}"
            yaml_path, counts = build_dataset(items, mode, project_labels, run_dataset, seed=42)
            metadata = {"project": {"id": int(project.id), "name": project.name},
                        "task_ids": ids, "label_ids": project_labels,
                        "class_mapping": {str(i): label_id for i, label_id in enumerate(sorted({obj["label_id"] for item in items for obj in item["objects"]}, key=lambda x: project_labels[x].casefold()))},
                        "images_selected": counts["selected"], "train_images": counts["train"],
                        "validation_images": counts["val"], "dataset": str(run_dataset),
                        "derived_boxes_from_polygons": derived,
                        "cvat_sdk_version": version("cvat-sdk")}
            checkpoint = train_model(yaml_path, mode, config, metadata)
            print(f"Treinamento concluído. Modelo salvo em: {checkpoint}")
        return 0
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

# CVAT + YOLO

Módulo independente para treinar modelos Ultralytics YOLO com tarefas anotadas no CVAT e enviar predições para tarefas escolhidas. Dependências, cache, datasets, checkpoints e logs ficam nesta pasta.

## Preparação

Requer Python 3.11 ou superior e `uv`. O SDK deve ter a mesma versão major/minor da API do servidor CVAT; este módulo usa `cvat-sdk` 2.62, alinhado aos outros módulos do repositório. Para outro servidor, ajuste a dependência no `pyproject.toml` para a versão compatível.

Na pasta `cvat_yolo`, configure `.env`:

```dotenv
CVAT_HOST=https://seu-cvat
CVAT_TOKEN=seu_token_pessoal
# Alternativamente, use CVAT_USERNAME e CVAT_PASSWORD.
YOLO_EPOCHS=100
YOLO_BATCH=16
YOLO_IMAGE_SIZE=640
YOLO_DEVICE=auto
CONFIDENCE_THRESHOLD=0.50
OVERWRITE_ANNOTATIONS=false
```

O token pessoal é preferível. Labels e modalidade são descobertos nas tarefas e no projeto; não são configurados no `.env`.

Execute os comandos com o diretório atual em `cvat_yolo`, para que `uv` use este `pyproject.toml` e crie o ambiente isolado do módulo.

## Treinar

```powershell
uv run python train.py
```

O fluxo lista projetos, mostra o estado real dos jobs e permite escolher apenas tarefas concluídas. Analisa shapes e labels do CVAT antes de perguntar Detection ou Segmentation. Detection aceita retângulos e pode derivar bounding boxes dos polígonos com confirmação; Segmentation usa polígonos e ignora retângulos somente após aviso e confirmação. Shapes, tracks e tags incompatíveis são relatados. O usuário escolhe todas as imagens compatíveis ou uma quantidade; a seleção usa seed 42.

O split usa tarefas distintas sempre que possível. Quando há apenas uma tarefa, separa blocos contíguos de frames para reduzir vazamento entre frames vizinhos. São necessários ao menos dois frames anotados compatíveis. O dataset gerado e o checkpoint são mantidos em `data/` e `models/<modalidade>/run_NNN/`; cada execução tem `metadata.json` e não sobrescreve as anteriores.

## Predizer e enviar

```powershell
uv run python predict.py
```

Escolha o projeto, as tarefas de destino e um checkpoint que tenha metadados. O programa carrega o modelo, compara os nomes das classes com as labels do projeto e solicita mapeamento para classes sem correspondência exata. Depois da confirmação, lê frames originais pelo CVAT SDK, executa inferência e envia retângulos ou polígonos via atualização aditiva do SDK.

Frames com shapes ou tags manuais são preservados e ignorados; tasks com tracks são ignoradas integralmente, pois o CVAT interpola tracks em frames sem keyframe explícito. Predições são marcadas como automáticas. Com `OVERWRITE_ANNOTATIONS=true`, o terminal pede confirmação adicional e substitui somente predições automáticas anteriores, depois de enviar novos resultados; anotações manuais permanecem protegidas. Sem essa confirmação, os frames automáticos existentes também são ignorados.

Cada execução é acrescentada a `logs/predictions.jsonl`. Falhas são reportadas por frame e não impedem o processamento dos demais frames.

## Verificações

```powershell
uv run python -m unittest discover -s tests -v
```

Os testes cobrem conversão de coordenadas e mapeamento de classes entre projetos. Eles não fazem chamadas ao CVAT nem substituem uma verificação de conexão com um servidor real.

# Instruções para o CVAT AI Toolkit Web

## Visão Geral do Projeto

Este repositório é dedicado exclusivamente à plataforma web **CVAT AI Toolkit** localizada na pasta `cvat_toolkit_web`. Toda a lógica de aquisição de câmeras Digifort, upload para o CVAT, treinamento de redes neurais YOLO (YOLO11) e auto-anotação é gerenciada diretamente através da aplicação web FastAPI.

---

## Estrutura da Aplicação (`cvat_toolkit_web`)

- `app/main.py`: Ponto de entrada FastAPI, montagem de rotas, static e gerenciamento de ciclo de vida.
- `app/core/`:
  - `acquisition_manager.py`: Motor assíncrono multithread dos workers de aquisição de imagens (Digifort/RTSP/HTTP), telemetria PTZ e persistência de checkpoints `.history.json`.
  - `job_queue.py`: Fila sequencial de execução única (FIFO) para treinos e predições com telemetria em tempo real e prevenção de Out-Of-Memory na GPU.
  - `cvat.py`: Integração com a API do CVAT via `cvat_sdk`.
  - `config.py`: Configurações centralizadas carregadas de `cvat_toolkit_web/.env`.
- `app/modules/`:
  - `acquisition/`: Interface e endpoints para gerenciar câmeras Digifort e workers ativos.
  - `training/`: Wizard de treinamento de modelos YOLO, análise de dataset, descarte de frames deletados e visualizador de modelos.
  - `prediction/`: Módulo de auto-anotação com limitação de instâncias por frame e transição automática para o estágio de validação no CVAT.
  - `jobs/`: Dashboard da fila de execução, histórico de jobs, logs ao vivo (SSE) e métricas de treinamento.
- `app/frameworks/yolo/`:
  - `trainer.py`: Motor de treinamento YOLO11 da Ultralytics.
  - `dataset.py`: Preparação de datasets YOLO com separação treino/val e exclusão de frames deletados.
  - `predictor.py`: Motor de inferência e envio de anotações ao CVAT.
- `data/`: Armazenamento persistente de workers, histórico de URLs e imagens coletadas.
- `models/`: Armazenamento de checkpoints de pesos treinados (`best.pt`) organizados por projeto e execução.

---

## Comandos Principais

### Executar a Aplicação Web
```bash
cd cvat_toolkit_web
uv run python run.py
```
Acessível nas portas **3012** (padrão) e **3013**.

### Executar Testes Automatizados
```bash
cd cvat_toolkit_web
uv run python -m unittest discover -s tests -v
```

---

## Regras Importantes de Desenvolvimento

1. **Autonomia da Web:** Todas as funcionalidades de aquisição, envio ao CVAT, treino e inferência residem em `cvat_toolkit_web`. Não crie pastas de scripts externas ou scripts paralelos soltos.
2. **Preservação de Dados:** Nunca apague a pasta `data/` ou `models/` em operações de limpeza.
3. **Frames Deletados no CVAT:** O CVAT não remove frames de `task.get_meta().size`, apenas adiciona os índices a `deleted_frames`. As contagens de imagens e montagem de dataset devem sempre ignorar frames em `deleted_frames`.
4. **Fila Sequencial de GPU:** Treinamentos e auto-anotações sempre passam pela fila gerenciada por `job_queue.py` para garantir que apenas um processo de GPU execute por vez.

---

## Documentação Técnica de Referência (`doc/`)

O repositório conta com manuais e documentações técnicas oficiais para consulta na pasta `doc/`:

- `doc/digifort/`: Manuais técnicos oficiais fornecidos pela Digifort para integração de APIs e streams:
  - `Digifort HTTP API.pdf`: Especificação detalhada de todos os endpoints HTTP REST do Digifort (`/Interface/Cameras/GetSnapshot`, busca de gravações, telemetria PTZ, presets e autenticação).
  - `Digifort RTSP Interface 7.3.0.pdf`: Especificação técnica de streams RTSP e sintaxe de URLs ao vivo (`/Interface/Cameras/Media?Camera=...`).
  - `XDigiPlaybackCore.pdf`: Métodos e parâmetros para controle e busca de gravações históricas no servidor Digifort.
  - `XDigiLiveCore.pdf`: Biblioteca e controles de vídeo ao vivo.
  - `EventVariables.pdf`: Dicionário de variáveis de eventos, alarmes e metadados de câmeras.
  - `SDK - Readme.pdf`: Arquitetura do SDK Digifort e convenções gerais.
  - `ActiveX for HTML pages.pdf`: Referência histórica de controles web.

> **Orientação para Agentes:** Sempre que for necessário investigar parâmetros de snapshots, sintaxe de URLs RTSP/HTTP, suporte a telemetria PTZ ou comportamento de busca em gravações passadas do Digifort, consulte os arquivos em `doc/digifort/`.

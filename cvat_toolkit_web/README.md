# CVAT AI Toolkit Web ⚡

Interface web moderna, rápida e neutra construída em **FastAPI**, **Jinja2**, **HTMX** e **Alpine.js** para treinamento de modelos de visão computacional (YOLO) e auto-anotação de tarefas no **CVAT**.

---

## 🏗️ Arquitetura e Separação de Responsabilidades

O projeto foi desenhado com foco em desacoplamento e facilidade de extensão para futuros frameworks e modelos (ex.: YOLOv8/11, RT-DETR, SAM):

```
cvat_toolkit_web/
├── app/
│   ├── core/                  # Conexão CVAT, configurações, SSE e interfaces abstratas
│   │   ├── base_framework.py  # BaseTrainer e BasePredictor (interfaces de IA)
│   │   ├── cvat.py            # SDK CVAT desacoplado
│   │   └── events.py          # Server-Sent Events (SSE) para logs em tempo real
│   │
│   ├── frameworks/            # 🧠 REGRA DE NEGÓCIO DE IA (ISOLADA)
│   │   └── yolo/              # Trainer, Predictor e manipulação de datasets YOLO
│   │
│   ├── modules/               # 📂 UMA PASTA DEDICADA POR ABA DO SISTEMA
│   │   ├── training/          # ABA 1: TREINAMENTO (Rotas, Services, Schemas e Templates)
│   │   ├── prediction/        # ABA 2: AUTO-ANOTAÇÃO (Rotas, Services, Schemas e Templates)
│   │   └── common/            # Layout base compartilhado e componentes universais
│   │
│   ├── static/                # CSS customizado neutro (Slate/Zinc) e JS local (HTMX/SSE)
│   └── main.py                # Entrada FastAPI
│
├── run.py                     # Script de inicialização rápida
├── pyproject.toml             # Gerenciado via uv
└── .env                       # Credenciais do CVAT e hiperparâmetros padrão
```

---

## 🚀 Como Executar

### 1. Configurar o `.env`
Copie `.env.example` para `.env` e configure o acesso ao seu servidor CVAT:
```dotenv
CVAT_HOST=http://localhost:8080
CVAT_TOKEN=seu_token_pessoal_aqui
# ou CVAT_USERNAME e CVAT_PASSWORD
```

### 2. Iniciar a Aplicação
Dentro da pasta `cvat_toolkit_web`, execute:
```bash
uv run python run.py
```
A aplicação iniciará na porta **3012** e estará acessível em:
- **Acesso Local**: http://localhost:3012
- **Acesso Remoto (Rede)**: http://10.247.168.43:3012

---

## 📋 Funcionalidades por Aba

### 🏋️ Aba 1: Treinamento & Gerenciamento de Modelos
1. **Submenu Dedicado**:
   - **Novo Treino (`/training`)**: Fluxo guiado em 5 passos para seleção de tarefas do CVAT, diagnóstico de anotações, configuração de framework/hiperparâmetros e monitoramento ao vivo.
   - **Modelos Treinados (`/training/models`)**: Lista todos os modelos salvos em disco agrupados/filtrados por framework (`yolo11`, etc.) e modalidade (`Detecção` e `Segmentação`), com métricas (mAP50, loss), classes e opção de **exclusão permanente** (`🗑️ Excluir`).
2. **Ciclo de Limpeza Automática pós-task**:
   - **Zero Lixo em Disco**: Ao finalizar o treino ou inferência (com sucesso ou falha), as pastas de datasets temporários (`data/dataset_*`) e os caches de frames (`data/cache/task_*`) são excluídos automaticamente no bloco `finally`.
   - **Preservação Inteligente**: Apenas os modelos base pré-treinados baixados (`*.pt` na raiz) e os checkpoints treinados finais (`models/<modo>/run_XXX/best.pt` + `metadata.json`) são mantidos em armazenamento permanente.
3. **Modalidade Flexível**: Suporta **Detecção** e **Segmentação**, com opção de converter polígonos em caixas delimitadoras (*bounding boxes*).
4. **Terminal ao Vivo**: Streaming em tempo real (*Server-Sent Events*) com barra de progresso, contador de épocas, perda (*loss*) e mAP50.

### 🎯 Aba 2: Auto-Anotação & Inferência
1. **Seleção de Modelos**: Varre e lista modelos treinados disponíveis em `models/` com metadados (classes, modalidade, data).
2. **Mapeamento Inteligente de Classes**: Associa classes detectadas pelo modelo às labels do projeto CVAT destino, com correspondência exata pré-selecionada.
3. **Proteção de Anotações**:
   - Anotações manuais são **100% preservadas e protegidas**.
   - Tarefas com tracks são ignoradas por segurança (para evitar conflito com interpolações).
   - Suporte opcional a substituição de anotações automáticas anteriores (`overwrite_auto`).
4. **Progresso Frame a Frame**: Exibe métricas de processamento, shapes enviados e histórico registrado em `logs/predictions.jsonl`.

### 📋 Aba 3: Fila de Tarefas (Background Worker FIFO)
1. **Lista Simplificada por Ordem de Chegada**: Visualização consolidada de todas as tarefas com identificação de ordem de chegada (`#1`, `#2`...), status claro e descrição detalhada dos parâmetros.
2. **Status Amigáveis**:
   - ⏳ **Em espera**: Aguardando na fila ou em execução no momento.
   - ✅ **Finalizado com sucesso**: Treinamento concluído ou predições enviadas sem erros.
   - ❌ **Finalizado com erro**: Falha com o motivo e mensagem de erro destacados.
   - ⚠️ **Cancelado**: Interrompido pelo usuário.
3. **Motor Assíncrono Nativo**: Execução sequencial estrita (1 worker por vez) para proteger os recursos de VRAM da GPU, com persistência em disco (`job_history.json`) e streaming em tempo real via Server-Sent Events (SSE).
4. **Documentação Completa de Arquitetura**: Para detalhes sobre o funcionamento do motor de background jobs e a comparação com Celery, consulte o [README da Arquitetura de Tarefas](README_ARQUITETURA_TASKS.md).

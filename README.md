# CVAT Digifort Image Labeling Toolkit

**Primeiro uso:** siga o [passo a passo para executar os dois scripts](README_USO.md).
O guia também explica por que manter coleta e envio separados.

Scripts Python para coleta de imagens de câmeras IP, integração com Digifort e
envio para projetos de anotação no CVAT. Ferramentas para image collection,
image labeling, datasets e computer vision, com novas integrações conforme a necessidade.

## Organização

| Pasta | Finalidade | Guia |
|---|---|---|
| `digifort-scripts` | Coleta HTTP/HTTPS de várias câmeras, ao vivo ou do histórico Digifort. | [Coletor](digifort-scripts/README.md) |
| `cvat_scripts` | Envio das pastas definidas em JSON ao CVAT, com projeto, lotes e retomada por câmera. | [Uploader](cvat_scripts/README.md) |
| `auto_task_cvat` | Scripts anteriores de captura e integração, utilizados como referência. | [Arquivos anteriores](auto_task_cvat/) |

```mermaid
flowchart LR
    A[Digifort ou câmera HTTP] --> B[Coletor]
    B --> C[Pasta da câmera]
    C --> D[Uploader: lote completo]
    D --> E[Projeto CVAT]
    E --> F[Confirmar imagens]
    F --> G[Remover lote local se habilitado]
```

O coletor e o uploader são processos separados. Não exigem banco de dados, Redis
ou filas. Cada pasta tem seu próprio `.env`, `pyproject.toml` e `uv.lock`.

## Requisitos

- Python 3.12+ e uv disponíveis no terminal.
- Acesso de rede às câmeras/Digifort e ao CVAT.
- Projeto CVAT existente, com labels e permissão de criar tasks.
- Para histórico, permissão de consultar e visualizar gravações no Digifort.

Execute os comandos dentro da pasta do script. O uv prepara as dependências.
O SDK do uploader está alinhado ao CVAT 2.62.0. Os comandos abaixo usam PowerShell.

## Configurar e executar o fluxo completo

### 1. Preparar os arquivos .env

Na raiz do repositório, copie os modelos somente se ainda não houver configuração:

```powershell
if (-not (Test-Path -LiteralPath 'digifort-scripts/.env')) {
    Copy-Item -LiteralPath 'digifort-scripts/.env.example' -Destination 'digifort-scripts/.env'
}
if (-not (Test-Path -LiteralPath 'cvat_scripts/.env')) {
    Copy-Item -LiteralPath 'cvat_scripts/.env.example' -Destination 'cvat_scripts/.env'
}
```

Edite os servidores, credenciais, câmeras e projeto. Os modelos são exemplos:
mantenha apenas os objetos desejados. Reinicie o processo após alterar seu `.env`.

### 2. Configurar o coletor

Exemplo de `digifort-scripts/.env` para recuperar os últimos 30 dias e continuar ao vivo:

```dotenv
CAMERAS='[
  {
    "url": "http://USUARIO:SENHA@SERVIDOR:8601/Interface/Cameras/GetSnapshot?Camera=VO2C01",
    "folder": "VO2C01",
    "interval_seconds": 60,
    "history": true,
    "start_date": 30,
    "end_date": null,
    "quality": 100,
    "resize": null,
    "history_search_hours": 6,
    "history_retry_seconds": 30
  }
]'

OUTPUT_DIR=./volume/image
REQUEST_TIMEOUT=10
```

Use o endpoint de snapshot HTTP do Digifort; o coletor atual não recebe RTSP
diretamente. Codifique caracteres especiais nas credenciais da URL, como `@` em
`%40`. Para uma câmera HTTP/HTTPS comum, informe a URL do snapshot e `history=false`.
Adicione objetos a `CAMERAS` para coletar de várias câmeras.

### 3. Configurar o uploader

Exemplo de `cvat_scripts/.env`, usando a mesma pasta:

```dotenv
CVAT_URL=http://SERVIDOR_CVAT:8080
CVAT_USERNAME=seu_usuario
CVAT_PASSWORD='sua_senha'
CVAT_ORGANIZATION=

DRY_RUN=true
LOOP=true
POLL_INTERVAL_SECONDS=5
AUTO_REMOVE=false

FOLDERS='[
  {
    "images_dir": "../digifort-scripts/volume/image/VO2C01",
    "project_id": 8,
    "images_per_task": 50,
    "auto_remove": true
  },
  {
    "images_dir": "../digifort-scripts/volume/image/VO2C02",
    "project_id": 12,
    "images_per_task": 100,
    "auto_remove": false
  }
]'
```

`FOLDERS` é uma lista JSON: cada objeto escolhe a pasta, o projeto e o tamanho do
lote. Para uma única pasta, mantenha apenas um objeto; para adicionar câmeras,
acrescente objetos. **A câmera é o nome da pasta**, como `VO2C01` e `VO2C02`.
Cada `images_dir` aponta diretamente para as imagens dessa câmera.
Cada `project_id` identifica um projeto existente; o script não cria projetos ou labels.
`auto_remove` controla a remoção por pasta. Se omitido, herda `AUTO_REMOVE` global.
Uma `organization` no objeto pode substituir `CVAT_ORGANIZATION` global.

O campo `folder` no coletor e a pasta final de `images_dir` devem corresponder:

```text
digifort-scripts/volume/image/VO2C01/
  .history.json
  2026-10-29_08-00-00.jpg
  2026-10-29_08-01-00.jpg
```

### 4. Abrir dois terminais

**Terminal 1**, a partir da raiz do repositório:

```powershell
cd digifort-scripts
uv run python main.py
```

**Terminal 2**, também a partir da raiz:

```powershell
cd cvat_scripts
uv run python main.py --dry-run
```

A simulação consulta o projeto e mostra o planejamento, sem criar tasks nem
remover arquivos. Com a pasta vazia, apenas informa que está aguardando.
Depois, para manter o envio ativo no segundo terminal:

```powershell
uv run python main.py --send
```

`--send` ativa o envio mesmo com `DRY_RUN=true`. Com `LOOP=true`, espera cinco
segundos entre ciclos. Use `--send --once` para verificar uma única vez.
`--dry-run` sempre executa uma vez e nunca apaga arquivos, mesmo com remoção habilitada.
Encerre cada processo com Ctrl+C.

## Datas, horários e resolução

Estas opções ficam no objeto de cada câmera do coletor:

| Objetivo | `start_date` | `end_date` | `history` |
|---|---|---|---|
| Últimos 30 dias, depois ao vivo | `30` | `null` | `true` |
| Últimos 30 dias e encerrar no presente | `30` | `0` | `true` |
| Da gravação mais antiga e continuar | `null` | `null` | `true` |
| Período fixo | `"2026-10-01T08:00:00"` | `"2026-10-02T18:00:00"` | `true` |
| Somente ao vivo, continuamente | `null` | `null` | `false` |

Números sem aspas são dias de 24 horas antes da inicialização; `0.5` são 12 horas.
As datas relativas são calculadas uma vez por execução. `end_date: 0` fixa o fim
no instante de início; fim `null` ou omitido mantém a coleta sem prazo final.
Datas ISO aceitam hora e fuso: `2026-10-01T08:00:00-03:00`. Sem fuso, usam o horário
local; máquina e servidor Digifort devem usar o mesmo fuso para consultar gravações.

`interval_seconds` define os horários das imagens por câmera. No histórico, os
downloads ocorrem sequencialmente sem esperar esse intervalo no relógio real.
A busca inicial usa janelas de seis horas de histórico, não esperas de seis horas reais.

Sem `resize`, preserva os bytes recebidos. `"resize": [1280, 720]` reduz no Python
mantendo a proporção. O snapshot ao vivo Digifort solicita Full HD por padrão;
o histórico solicita o tamanho original se `capture_resolution` for omitido.
A qualidade padrão é 100. Veja os [dez exemplos de coleta](digifort-scripts/.env.example).

## Lotes, retomada e remoção

As tasks recebem câmera e datas da primeira/última imagem nos nomes:

```text
VO2C01 | 2026-10-29T08:00:00.000000 | 2026-10-29T08:49:00.000000
```

Com `images_per_task=50`:

| Imagens novas | Resultado |
|---|---|
| 0 | Aguarda a coleta. |
| 49 | Aguarda completar 50. |
| 50 | Envia uma task. |
| 101 | Envia duas tasks e deixa uma imagem aguardando mais 49. |

**Não envia lotes incompletos**, nem com `--once`. Se uma coleta finita deixar
um restante, ele fica na pasta. Para enviá-lo, ajuste temporariamente
`images_per_task` para a quantidade restante e execute `--send --once`; depois
restaure o limite habitual. O uploader em loop continua aguardando mesmo quando
o coletor termina um período; encerre-o com Ctrl+C quando necessário.

**Retomada da coleta:** usa a última imagem + intervalo, preservando progresso
válido mais adiante em `.history.json`. Em todas as câmeras, esse JSON registra
o nome da pasta, o arquivo e a data da última imagem salva, além do próximo horário.
Se todas as imagens já tiverem sido removidas após os envios,
retoma pelo JSON. Preserve esse arquivo. Sem imagens nem progresso, usa o início
configurado ou procura a gravação mais antiga.

**Retomada do envio:** consulta as tasks da câmera no projeto e encontra a maior
data final. Envia somente imagens posteriores; anteriores ou iguais são ignoradas,
inclusive lacunas antigas. Mantenha os nomes das tasks coerentes com seu conteúdo.
Para reprocessar um período anterior, use uma identificação de pasta/câmera ou
projeto que não tenha um ponto de retomada posterior ao período desejado.

**Remoção:** com `auto_remove: true` na pasta, somente os arquivos do lote recém-confirmado
podem ser apagados, após conferir quantidade, nomes no CVAT e finalizar o nome
da task. Arquivos alterados durante o upload são mantidos com aviso. Restantes,
imagens antigas ignoradas, `.part`, `.history.json` e outros arquivos permanecem.
Com `auto_remove: false`, os arquivos daquela pasta são preservados. Se o campo
for omitido, vale `AUTO_REMOVE` global.

## Outra câmera ou outro projeto

O coletor aceita várias câmeras, e o uploader verifica todas as entradas de
`FOLDERS` no mesmo processo. Adicione outra pasta à lista JSON:

```json
{
  "images_dir": "../digifort-scripts/volume/image/VO2C03",
  "project_id": 15,
  "images_per_task": 200,
  "auto_remove": true
}
```

Reinicie o uploader para carregar a lista alterada. A mesma execução pode enviar
para projetos diferentes ou para o mesmo projeto, mantendo cada câmera separada.
Uma falha em uma pasta não impede verificar as seguintes. As pastas são processadas
sequencialmente; a espera de cinco segundos ocorre após o ciclo completo.
Não duplique caminhos na lista ou nomes de câmera no mesmo projeto/organização.
Evite processos simultâneos para o mesmo destino.

## Falhas e problemas comuns

| Situação | O que conferir |
|---|---|
| CVAT recusa login | URL, usuário/senha e acesso da conta. |
| Projeto não encontrado | `project_id`, organização e permissões. |
| Imagens abaixo do tamanho do lote | Aguarda completar `images_per_task`. |
| Pasta permanece vazia | URL da imagem, início agendado, rede e logs do coletor. |
| Task permanece com `uploading` | Verifique processamento, quantidade e arquivos no CVAT antes de concluir ou corrigir a task. |
| Task antiga não identifica a câmera | Revise e renomeie com câmera, início e fim; o uploader mostra os IDs ambíguos. |
| Imagens antigas não são enviadas | Estão antes do último horário das tasks dessa câmera/projeto. |
| Arquivo confirmado não foi removido | Confira avisos de arquivo alterado, permissões ou arquivo em uso. |

Uma task nova tem temporariamente o sufixo ` | uploading`. Falhas preservam o
lote local e esse marcador impede avançar sem revisão. Não conclua manualmente
uma task incompleta só para liberar o envio. No modo contínuo, os erros são
registrados e há nova verificação no próximo ciclo.

## Referências e testes

- [Guia completo do coletor](digifort-scripts/README.md).
- [Guia completo do uploader](cvat_scripts/README.md).
- [Exemplos de coleta](digifort-scripts/.env.example).
- [Exemplos de envio](cvat_scripts/.env.example).
- [Documentação local do Digifort](digifort-scripts/digifort-doc/).

Para executar os testes locais, em cada pasta de script:

```powershell
uv run python -m unittest discover -s tests -v
```

Os testes do uploader usam cliente simulado e arquivos temporários isolados:
não criam tasks reais nem removem imagens operacionais.

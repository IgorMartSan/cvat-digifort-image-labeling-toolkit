# Como usar: coleta de imagens e envio ao CVAT

## Recomendação: manter dois scripts

Mantenha o coletor e o uploader separados. Eles compartilham somente a pasta de
imagens; cada processo pode ser configurado, reiniciado e acompanhado de forma independente.

| Situação | Com dois scripts |
|---|---|
| CVAT indisponível | A coleta continua gravando imagens na pasta local. |
| Câmera indisponível | O uploader pode enviar os lotes completos que já existem. |
| Alterar o projeto CVAT ou o tamanho do lote | Reinicie apenas o uploader. |
| Usar outra plataforma de labeling | Aproveite o mesmo coletor e implemente outro envio. |

A separação combina com a proposta deste repositório: uma coleção de ferramentas
reutilizáveis. Um script único precisaria coordenar coleta, envio e falhas de
ambos. Se futuramente for necessário iniciar tudo com um comando, um lançador
pode abrir os dois processos sem juntar suas responsabilidades.

Enquanto o CVAT estiver fora do ar, as imagens se acumulam no disco. Acompanhe
o espaço disponível: a pasta local mantém os lotes que ainda não foram confirmados.

## 1. Preparar o ambiente

Tenha Python 3.12+ e uv disponíveis. Você também precisa do endereço e acesso ao
Digifort, além de um projeto existente no CVAT com seus labels e permissões.

Abra o terminal na raiz do repositório. Crie os `.env` somente se ainda não existirem:

```powershell
if (-not (Test-Path -LiteralPath 'digifort-scripts/.env')) {
    Copy-Item -LiteralPath 'digifort-scripts/.env.example' -Destination 'digifort-scripts/.env'
}
if (-not (Test-Path -LiteralPath 'cvat_scripts/.env')) {
    Copy-Item -LiteralPath 'cvat_scripts/.env.example' -Destination 'cvat_scripts/.env'
}
```

## 2. Configurar a câmera

Edite `digifort-scripts/.env`. Este exemplo coleta uma imagem por minuto da câmera
`VO2C01`, recupera os últimos 30 dias disponíveis e depois continua ao vivo:

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
    "resize": null
  }
]'

OUTPUT_DIR=./volume/image
REQUEST_TIMEOUT=10
```

Substitua usuário, senha, servidor e câmera. Na URL, codifique caracteres especiais
das credenciais: `@` vira `%40`. A URL deve ser HTTP/HTTPS de snapshot, não RTSP.

| O que você deseja | Ajuste dentro do objeto da câmera |
|---|---|
| Desde a gravação mais antiga | `"history": true, "start_date": null` |
| Últimos 30 dias e continuar | `"history": true, "start_date": 30, "end_date": null` |
| Últimos 30 dias e encerrar no presente | `"history": true, "start_date": 30, "end_date": 0` |
| Somente imagens atuais | `"history": false, "start_date": null, "end_date": null` |
| Período fixo | Datas como `"2026-10-01T08:00:00"` e `"2026-10-02T18:00:00"` |

Para adicionar câmeras, acrescente objetos à lista e use uma pasta diferente para cada uma.

## 3. Configurar o envio

Edite `cvat_scripts/.env`:

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

Defina as pastas na lista JSON `FOLDERS`. Cada objeto informa o caminho,
o projeto de destino e o tamanho do lote. Para uma pasta, use um único objeto;
para várias, acrescente objetos. O exemplo envia `VO2C01` ao projeto 8 e `VO2C02`
ao projeto 12. Mantenha apenas as pastas que deseja acompanhar.
Se o projeto for de uma organização, preencha `CVAT_ORGANIZATION` global ou
`"organization": "slug"` no objeto correspondente.

O nome da **pasta final de `images_dir` identifica a câmera**. Neste exemplo, ela é
`VO2C01`, igual ao `folder` do coletor. Um único uploader percorre todas as pastas
configuradas, mantendo a retomada de cada câmera/projeto independente.

`"auto_remove": true` habilita a remoção daquela pasta após a confirmação do
upload. Para preservar suas imagens, use `false`. Se omitido no objeto, vale
`AUTO_REMOVE` global. Sem `images_per_task`, o padrão é 50 imagens.

## 4. Iniciar o coletor

No **terminal 1**, a partir da raiz do repositório:

```powershell
cd digifort-scripts
uv run python main.py
```

O uv instala as dependências. Confira os logs e os arquivos criados em
`digifort-scripts/volume/image/VO2C01`.

## 5. Conferir e iniciar o uploader

No **terminal 2**, também a partir da raiz:

```powershell
cd cvat_scripts
uv run python main.py --dry-run
```

A simulação não cria tasks nem remove arquivos. Quando há imagens, consulta o
CVAT para verificar a retomada. Com a pasta vazia, aguarda sem testar o login.

Depois, no mesmo terminal, inicie o envio contínuo:

```powershell
uv run python main.py --send
```

Mantenha os dois terminais abertos. O uploader verifica cada pasta em sequência
e espera cinco segundos após o ciclo completo. Uma falha em uma pasta não impede
as seguintes. `--send` ativa o envio mesmo com `DRY_RUN=true` no arquivo.

## 6. Entender o funcionamento

Com `images_per_task=50`, ele espera juntar 50 imagens novas, ordena por data e
cria uma task com câmera, primeira e última data no nome:

```text
VO2C01 | 2026-10-29T08:00:00.000000 | 2026-10-29T08:49:00.000000
```

Após o processamento, confere a quantidade e os nomes das imagens no CVAT. Só
depois finaliza a task e, se habilitado, remove os arquivos daquele lote.
O próximo ciclo continua após a maior data final das tasks dessa câmera no projeto.

- Com 49 imagens novas, espera mais uma.
- Com 101, envia duas tasks de 50 e deixa uma imagem aguardando mais 49.
- Em caso de falha, preserva o lote local. Uma task com ` | uploading` precisa
  de revisão antes de continuar, para não duplicar ou pular um upload incompleto.
- `.history.json`, arquivos `.part` e imagens fora do lote confirmado não são apagados.

O envio sempre avança: imagens anteriores ou iguais ao último horário enviado
são ignoradas, inclusive se forem acrescentadas à pasta depois.

## 7. Parar, reiniciar ou alterar a configuração

Use Ctrl+C em cada terminal. Para reiniciar, execute novamente os mesmos comandos.
O coletor retoma pela última imagem ou pelo progresso em `.history.json`; o uploader
retoma pelas tasks existentes no projeto. Preserve o JSON, especialmente com remoção automática.

O `.history.json` de cada pasta registra `folder`, `last_image`, `last_image_at`
e `next_capture_at`, inclusive para câmeras sem histórico. Esse arquivo mantém
a referência da última imagem mesmo depois de ela ser removida da pasta.

Alterações no `.env` precisam de reinicialização do processo correspondente.
Evite iniciar dois uploaders para a mesma câmera no mesmo projeto.

Para enviar uma única vez, mantendo a exigência de lote completo:

```powershell
uv run python main.py --send --once
```

Se uma coleta com data final terminar com menos imagens que o lote, o restante
continua aguardando. Para enviá-lo, ajuste temporariamente `images_per_task` para
a quantidade restante, use `--send --once` e depois restaure o valor habitual.

## Mais detalhes

### Perfil configurado para o primeiro dataset YOLO

As câmeras `VO2C01`, `VO2C02` e `VO2C05` foram configuradas nos `.env` locais com
`start_date: 30`, `interval_seconds: 600`, `history: true` e `end_date: null`.
O limite de tamanho é 1280×720, preservando a proporção, com qualidade JPEG 95.
No teste, `VO2C01` e `VO2C05` retornaram 1280×720; `VO2C02`, 960×720 (4:3).

A saída configurada é `volume/image/<camera>` na raiz do repositório
(`OUTPUT_DIR=../volume/image` no coletor). As três entradas do uploader apontam
para essas mesmas pastas, no projeto 8, em lotes de 50 imagens.
Imagens/progresso existentes mantêm prioridade sobre a data inicial configurada.
Reinicie os dois processos após aplicar essas configurações.

O tamanho de entrada do modelo é definido separadamente pelo parâmetro `imgsz`
no [treinamento Ultralytics YOLO](https://docs.ultralytics.com/modes/train/).

- [Visão geral e problemas comuns](README.md).
- [Todas as opções do coletor](digifort-scripts/README.md).
- [Todas as opções do uploader](cvat_scripts/README.md).
- [Exemplos de coleta](digifort-scripts/.env.example).
- [Exemplos de envio](cvat_scripts/.env.example).

# Envio de imagens de várias pastas para o CVAT

Para configurar os dois processos juntos, veja o [guia do repositório](../README.md).

O nome da **pasta com as imagens é o nome da câmera**. Configure uma lista JSON
`FOLDERS` no `.env`: cada objeto escolhe sua pasta, projeto CVAT, quantidade por
task e remoção opcional. Um único processo acompanha todas as entradas, com
retomada independente pelos nomes das tasks de cada câmera no respectivo projeto.

## Configuração

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

Nesse exemplo, `VO2C01` usa o projeto 8 e lotes de 50; `VO2C02` usa o projeto 12 e
lotes de 100. Para uma pasta, mantenha somente um objeto. As imagens devem estar
diretamente dentro de cada pasta:

```text
VO2C01/
  2026-10-29_08-00-00.jpg
  2026-10-29_08-01-00.jpg
  2026-10-29_08-02-00_123456.jpg
```

Não há configuração separada de nome da câmera. Cada `images_dir` deve ser a pasta
de uma câmera, não a raiz contendo várias câmeras. Para adicionar outra, acrescente
um objeto à lista e reinicie. Projetos diferentes podem aparecer na mesma lista.

| Campo | Onde configurar | Uso |
|---|---|---|
| `CVAT_URL` | Global | Endereço HTTP/HTTPS do CVAT, sem `/api`. |
| `CVAT_USERNAME` / `CVAT_PASSWORD` | Global | Conta com acesso de leitura e criação de tasks nos projetos. |
| `CVAT_ORGANIZATION` | Global | Organização padrão; pode ser substituída por pasta. |
| `FOLDERS` | Global | Lista JSON com um objeto por pasta. |
| `images_dir` | Objeto JSON | Caminho da pasta da câmera. |
| `project_id` | Objeto JSON | ID do projeto existente para essa pasta. |
| `images_per_task` | Objeto JSON | Lote completo; padrão 50 se omitido. |
| `auto_remove` | Objeto JSON | Booleano; se omitido, herda `AUTO_REMOVE`. |
| `organization` | Objeto JSON | Substitui `CVAT_ORGANIZATION`; vazio usa o contexto padrão. |
| `DRY_RUN` | Global | `true` simula; `false` envia se não houver argumento. |
| `LOOP` | Global | `true` repete os ciclos; `false` percorre a lista uma vez. |
| `POLL_INTERVAL_SECONDS` | Global | Espera após percorrer todas as pastas; padrão 5 segundos. |
| `AUTO_REMOVE` | Global | Padrão usado quando não há `auto_remove` no objeto. |

Use booleanos JSON `true`/`false`, sem aspas, e não coloque comentários dentro
da lista. Uma mesma pasta não pode aparecer duas vezes. Pastas diferentes com o
mesmo nome de câmera no mesmo projeto/organização também são rejeitadas, pois
compartilhariam o mesmo ponto de retomada. O mesmo nome em projetos diferentes é permitido.

Caminhos relativos partem da pasta do script. Caminhos Windows podem usar `/`, por exemplo `C:/datasets/VO2C01`. O `.env` é ignorado pelo Git. As credenciais e o destino inicialmente copiados de `auto_task_cvat` devem ser conferidos antes de usar.

## Executar

Requer Python 3.12+ e uv. Dentro de `cvat_scripts`:

```powershell
# Consultar o projeto e mostrar o planejamento, sem criar tasks.
uv run python main.py --dry-run

# Consultar, verificar a retomada e enviar os próximos lotes.
uv run python main.py --send

# Uma única verificação, enviando somente os lotes completos disponíveis.
uv run python main.py --send --once
```

O uv instala as dependências. Sem argumentos, segue `DRY_RUN`. A simulação também autentica no CVAT para consultar as tasks existentes. O projeto e seus labels devem existir; as tasks herdam os labels do projeto. O script não cria projetos.

Com `LOOP=true`, `--send` mantém o uploader ativo. Percorre as pastas sequencialmente
e espera `POLL_INTERVAL_SECONDS` após o ciclo completo. Uma falha não impede
verificar as próximas entradas. Pasta vazia é normal:
aguarda novas imagens, sem conectar ao CVAT nesse ciclo. Se a pasta não existir,
ela é criada. `--dry-run` sempre verifica uma única vez e nunca remove arquivos.
Ctrl+C encerra o processo. Erros de conexão e avisos de upload pendente não derrubam
o modo contínuo: o próximo ciclo tenta verificar novamente.

## Coletor e uploader ao mesmo tempo

Use a mesma pasta de câmera nos dois scripts. No `.env` do uploader, configure:

```dotenv
LOOP=true
POLL_INTERVAL_SECONDS=5
AUTO_REMOVE=true
```

Esse é o padrão global de remoção; um `auto_remove` explícito em um objeto de
`FOLDERS` prevalece. Para habilitar ou desabilitar por câmera, ajuste esse campo
diretamente no JSON.

Em um terminal, dentro de `digifort-scripts`, execute `uv run python main.py`.
Em outro, dentro de `cvat_scripts`, execute `uv run python main.py --send`.
O coletor grava imagens completas por renomeação; o uploader ignora `.part`,
espera o lote completar e só então envia e remove as imagens confirmadas.
O campo `folder` da câmera no coletor deve corresponder ao nome da pasta indicada
em `images_dir` no uploader. Para captura sem fim, deixe `end_date` do coletor vazio/null.

O coletor persiste em `.history.json` o nome da pasta, o arquivo e a data da última
imagem salva, tanto no histórico quanto ao vivo. Assim, pode retomar mesmo se todas as imagens já tiverem sido
removidas pelo uploader. Esse JSON não é removido pelo envio.

## Nome e divisão das tasks

O padrão é:

```text
CAMERA | primeira-data-hora | ultima-data-hora
VO2C01 | 2026-10-29T08:00:00.000000 | 2026-10-29T08:49:00.000000
```

Cada nome contém a câmera e os horários da primeira e da última imagem daquele lote. Os horários vêm dos arquivos, não da data de criação/modificação. As imagens são ordenadas cronologicamente antes do envio.

Com 101 imagens novas e `images_per_task=50`, cria duas tasks de 50 e deixa uma
imagem aguardando mais 49. **Lotes incompletos nunca são enviados**, inclusive em
`--once`. Esse limite é por task, não por job. Imagens com exatamente o mesmo
timestamp ficam juntas; se o limite dividir um desses grupos, o script solicita
ajustar o limite ou usar nomes com microssegundos distintos.

Se a coleta tiver data final e deixar um restante, o uploader seguirá aguardando.
Para enviar somente esse restante, ajuste temporariamente `images_per_task` para
sua quantidade, execute `--send --once` e depois restaure o tamanho habitual.

Formatos aceitos, incluindo o coletor antigo:

```text
2026-10-29_08-00-00.jpg
2026-10-29_08-00-00_123456.jpg
2026_10_29_08_00_00.png
2026_10_29_08_00_00_123456.png
```

Extensões: JPG/JPEG, PNG, WebP, BMP, TIF/TIFF e GIF. Arquivos vazios ou sem data válida são ignorados com aviso. Temporários `.part` não são enviados. Nomes de câmera aceitam letras ASCII, números, `_` e `-`.

## Retomada pelo último envio

1. Consulta todas as páginas de tasks somente do projeto configurado.
2. Seleciona as tasks da câmera identificada pelo nome da pasta.
3. Encontra a **maior data final** entre essas tasks, independentemente da ordem ou do ID.
4. Ignora todas as imagens locais anteriores ou iguais a esse horário.
5. Cria lotes somente com as imagens seguintes.

Exemplo: a task mais recente de `VO2C01` termina em `2026-10-29T08:49:00`. Na próxima execução, começa na primeira imagem local posterior a 08:49:00. Imagens mais antigas são ignoradas, mesmo que exista uma lacuna entre tasks. Alterar `images_per_task` não muda esse ponto de retomada.

Se ainda não houver tasks dessa câmera no projeto, começa na primeira imagem da pasta. Se todas as imagens já forem anteriores ou iguais ao último envio, avisa e não cria tasks. Tasks de outras câmeras não alteram a retomada.

A verificação usa os nomes das tasks, sem banco de dados ou marcador local. Mantenha esses nomes coerentes com o conteúdo. Arquivos antigos adicionados depois do último envio não entram automaticamente, pois o fluxo sempre continua para a frente.

## Tasks antigas e uploads incompletos

Para tasks antigas fora do padrão, tenta identificar a câmera pelo nome da task ou pelo caminho das imagens e lê as datas dos arquivos. Se houver possível conflito e a câmera não puder ser identificada, mostra os IDs e bloqueia o envio para revisão.

Confira a câmera e o período reais e renomeie essas tasks no CVAT:

```text
VO2C01 | 2026-10-29T08:00:00 | 2026-10-29T08:49:00
```

Tasks dessa câmera sem imagens prontas também bloqueiam a continuação, para que o script não pule um lote cujo upload falhou. Revise a task indicada antes de reenviar. O script não renomeia tasks antigas nem apaga tasks automaticamente.

Durante a criação, uma task nova recebe temporariamente o sufixo ` | uploading`.
Após o upload, o script consulta a quantidade e os nomes de todas as imagens no
CVAT e só então remove esse sufixo. Uma task com o sufixo ainda presente bloqueia
novos envios da câmera, mesmo que tenha algumas imagens, até a revisão/conclusão.
O script não remove esse marcador automaticamente após uma falha.

## Remoção automática

Com `auto_remove: true` na entrada da pasta, a remoção ocorre depois de o upload/processamento
terminar, a quantidade e os nomes corresponderem ao lote e o nome final da task
ser confirmado. Remove apenas os caminhos daquele lote. Imagens esperando completar
outro lote, `.part`, `.history.json` e outros arquivos permanecem.

Se um arquivo mudar durante o envio, ele é mantido com aviso. Se houver erro ao
remover um arquivo, ele é mantido para revisão. Imagens antigas ignoradas pela
retomada não são apagadas automaticamente: não basta sua data ser anterior ao
último envio. A simulação nunca remove arquivos.

## Verificações e limites

Antes de cada criação, consulta o projeto novamente. Se surgir outra task que avance o ponto de retomada, interrompe para recalcular na próxima execução. Execute uma instância por projeto/câmera: o CVAT não impõe unicidade aos nomes, então duas instâncias simultâneas ainda podem criar tasks iguais entre a consulta e a criação.

O upload usa qualidade 100, ordem predefinida e aguarda o processamento do CVAT.
Se falhar, informa o ID criado e preserva os arquivos locais. No modo contínuo,
as verificações continuam a cada ciclo, mas o marcador de upload pendente impede
duplicar o lote; revise essa task no CVAT. Com `LOOP=false` ou `--once`, uma falha
encerra a execução. Somente lotes confirmados podem ser removidos por `AUTO_REMOVE`.

O SDK está alinhado ao servidor consultado: CVAT 2.62.0 (`cvat-sdk~=2.62.0`). Para outro servidor, confira a versão correspondente do SDK.

## Testes

```powershell
uv run python -m unittest discover -s tests -v
```

Os testes de upload usam cliente simulado e não criam tasks reais.

Referência: [SDK oficial do CVAT](https://docs.cvat.ai/docs/api_sdk/sdk/highlevel-api/).

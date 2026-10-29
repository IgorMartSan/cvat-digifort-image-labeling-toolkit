from cvat_sdk import make_client
import os

class CVATClient:
    def __init__(self, cvat_url: str, username: str, password: str):
        """
        Inicializa o cliente CVAT com as credenciais de login.

        Parâmetros:
        cvat_url (str): URL do servidor CVAT.
        username (str): Nome de usuário para autenticação.
        password (str): Senha para autenticação.
        """
        self.cvat_url = cvat_url
        self.username = username
        self.password = password

        

    def listar_projetos(self):
        """
        Lista todos os projetos do CVAT.

        Retorna:
        list: Uma lista de dicionários contendo os detalhes dos projetos.
        """
        projetos = []
        try:
            # Cria um cliente CVAT autenticado
            with make_client(host=self.cvat_url) as client:
                client.login((self.username, self.password))

                # Lista todos os projetos
                projects = client.projects.list()

                # Adiciona os detalhes dos projetos à lista de retorno
                for project in projects:
                    projetos.append({
                        "ID": project.id,
                        "Nome": project.name,
                        "Status": project.status
                    })

        except Exception as e:
            print(f"Erro ao listar projetos: {e}")

        return projetos

    def criar_task_com_imagens(self, project_id: int, task_name: str, image_paths: list, organization_slug: str):
        """
        Cria uma task no CVAT com base no ID do projeto e um array de imagens.

        Parâmetros:
        project_id (int): ID do projeto onde a task será criada.
        task_name (str): Nome da task a ser criada.
        image_paths (list): Lista de caminhos para os arquivos de imagem.

        Retorna:
        dict: Detalhes da task criada, ou mensagem de erro.
        """
        try:
            # Cria um cliente CVAT autenticado
            with make_client(host=self.cvat_url) as client:
                client.login((self.username, self.password))

                client.organization_slug = organization_slug

                # Prepara o payload para a criação da task
                task_spec = {
                    "name": task_name,
                    "project_id": project_id,
                }

                # Verifica se todos os caminhos das imagens são válidos
                for image_path in image_paths:
                    if not os.path.isfile(image_path):
                        raise FileNotFoundError(f"A imagem '{image_path}' não foi encontrada.")

                # Cria a task no CVAT
                task = client.tasks.create_from_data(
                    spec=task_spec, 
                    resources=image_paths,  # Passamos a lista de caminhos para as imagens
                    data_params={"quality": 100}
                )

                print(f"Task '{task.name}' criada com sucesso! ID da Task: {task.id}")
                return {
                    "ID": task.id,
                    "Nome": task.name,
                    "Projeto": task.project_id,
                    "Status": task.status
                }

        except Exception as e:
            print(f"Erro ao criar a task: {e}")
            return None


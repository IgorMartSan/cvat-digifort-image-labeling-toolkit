import os
import time
import cv2
import traceback
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from utils.cvat_requests import CVATClient  # Assumindo que você tenha um cliente CVAT configurado

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Configurações da câmera e CVAT
CAMERA_URL = r'rtsp://ax12813:ims@2024@10.247.72.55/Interface/Cameras/Media?Camera=VO2C01'
IMAGE_SAVE_DIR = "./volumes/auto_task_cvat"
WAIT_INTERVAL = 10 * 60  # Captura de imagem a cada 60 segundos
RECONNECT_INTERVAL = 3  # Tentar reconectar a cada 3 segundos
NUM_IMG_PER_TASK = 50  # Enviar para CVAT quando atingir 100 imagens

CVAT_URL = os.environ["CVAT_URL"]
USERNAME = os.environ["CVAT_USERNAME"]
PASSWORD = os.environ["CVAT_PASSWORD"]
PROJECT_ID = 8
organization_slug = "IgorProjetos"
# Criar diretório para salvar imagens, se não existir

def create_directory_if_not_exists(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)

# Limpar diretório de imagens enviadas
def clear_directory(directory):
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')

# Salvar imagem capturada
def save_image(image, path):
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    image_name = f"{timestamp}.png"
    image_path = os.path.join(path, image_name)
    cv2.imwrite(image_path, image)
    print(f"Image saved: {image_path}")
    return image_path

# Obter caminhos das imagens no diretório
def get_image_paths(directory):
    return [os.path.join(directory, file) for file in os.listdir(directory) if file.endswith(('.jpg', '.png'))]

# Contar número de imagens no diretório
def get_image_count(directory):
    return len(get_image_paths(directory))

# Função principal
def main():
    create_directory_if_not_exists(IMAGE_SAVE_DIR)
    client = CVATClient(cvat_url=CVAT_URL, username=USERNAME, password=PASSWORD)
    
    while True:
        cap = None
        # Tentar conectar à câmera
        while cap is None or not cap.isOpened():
            print("Attempting to connect to the camera...")
            cap = cv2.VideoCapture(CAMERA_URL)
            if not cap.isOpened():
                print("Failed to connect. Retrying in 3 seconds...")
                time.sleep(RECONNECT_INTERVAL)
            else:
                print("Camera connected successfully!")

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("Lost connection to the camera. Attempting to reconnect...")
                    cap.release()
                    break
                
                save_image(frame, IMAGE_SAVE_DIR)
                time.sleep(WAIT_INTERVAL)  # Espera 60 segundos antes da próxima captura
                
                # Verificar se atingimos o limite de imagens antes de enviar para CVAT
                num_img_dir = get_image_count(IMAGE_SAVE_DIR)
                print(f"Numero de imagens: {num_img_dir} de {NUM_IMG_PER_TASK}")
                if num_img_dir >= NUM_IMG_PER_TASK:
                    print("Criando nova task no CVAT...")
                    image_paths = get_image_paths(IMAGE_SAVE_DIR)
                    task_name = f"Camera Capture {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    
                    if client.criar_task_com_imagens(image_paths=image_paths, project_id=PROJECT_ID, task_name=task_name, organization_slug=organization_slug):
                        print("Images successfully uploaded to CVAT.")
                        clear_directory(IMAGE_SAVE_DIR)
                    else:
                        print("Error uploading images to CVAT.")
        
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()
            cap.release()
            time.sleep(RECONNECT_INTERVAL)

if __name__ == "__main__":
    main()

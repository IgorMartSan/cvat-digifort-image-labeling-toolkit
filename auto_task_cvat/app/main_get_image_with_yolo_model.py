import os
import time
import cv2
import traceback
from datetime import datetime
from utils.cvat_requests.py import CVATClient  # Assumindo que você tenha um cliente CVAT configurado
from utils.time_hysteresis import TimeHysteresis

# Configurações da câmera e CVAT
CAMERA_URL = r'rtsp://ax12813:ims@2024@10.247.72.55/Interface/Cameras/Media?Camera=VO2C01'
IMAGE_SAVE_DIR = "./volumes/auto_task_cvat"
WAIT_INTERVAL = 10 * 60  # Captura de imagem a cada 60 segundos
RECONNECT_INTERVAL = 3  # Tentar reconectar a cada 3 segundos
NUM_IMG_PER_TASK = 50  # Enviar para CVAT quando atingir 100 imagens

CVAT_URL = "http://10.247.141.99:8080"
USERNAME = "sender@aperam.com"
PASSWORD = "123456.abC"
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

                ##############################################################
       
               
                time.sleep(WAIT_INTERVAL)  # Espera 60 segundos antes da próxima captura
                 # Model
                results = model(frame, verbose=False, max_det = 1)
                frame = results[0].plot() #Para ver o plot do yolo

                # frame = annotated_frame
                boxes = results[0].boxes
                masks = results[0].masks
                names = results[0].names

                for result in results:
                    names = result.names
                    boxes = result.boxes  # Boxes object for bounding box outputs
                    masks = result.masks  # Masks object for segmentation masks outputs
                    keypoints = result.keypoints  # Keypoints object for pose outputs
                    probs = result.probs  # Probs object for classification outputs
                    obb = result.obb  # Oriented boxes object for OBB outputs
                    result.show()  # display to screen
                    result.save(filename="result.jpg")  # save to disk
                    y1 = 99999
                    if boxes is not None:
                        for box in boxes:
                            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                            cls_id = int(box.cls[0])
                            conf = float(box.conf[0])
            

                tables = postgresConnection.list_tables()
                print(tables)

                if y1 <= threshold_y:
                    print(f"Alerta: O objeto  ultrapassou o limite!")
                    timeHysteresis.update(True)
                else:
                    print(f"Normal: O objeto no limite!")
                    timeHysteresis.update(False)

                if timeHysteresis.has_changed():
                    if timeHysteresis.previous_state == False and timeHysteresis.current_state == True:
                        label_img = "Alerta"
                        labal_color = (0, 0, 255)
                        event_id = postgresConnection.insert_event_by_cam_name(cam_name="xpto")

                    elif timeHysteresis.previous_state == True and timeHysteresis.current_state == False:
                        label_img = "Normal"
                        labal_color = (0, 255, 0)
                        postgresConnection.update_event_end_now_by_event_id(event_id=event_id)


                # Lógica para salvar imagens a cada 70 frames durante o evento ativo
                if timeHysteresis.previous_state == False and timeHysteresis.current_state == True:
                    # Início do evento
                    label_img = "Alerta"
                    labal_color = (0, 0, 255)
                    event_active = True
                    frames_since_event_start = 0
                    images_saved_count = 0
                    timestamp = time.strftime('%Y_%m_%d_%H_%M_%S')
                    current_event_folder = os.path.join(IMG_PATH, f"event_{event_id}_{timestamp}")
                    os.makedirs(current_event_folder, exist_ok=True)













                

                ##############################################################

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

from app.frameworks.yolo import YoloTrainer, YoloPredictor

yolo_trainer = YoloTrainer()
yolo_predictor = YoloPredictor()

TRAINERS = {
    "yolo11": yolo_trainer,
    "yolo26": yolo_trainer,
    "yolo": yolo_trainer,
}

PREDICTORS = {
    "yolo11": yolo_predictor,
    "yolo26": yolo_predictor,
    "yolo": yolo_predictor,
}

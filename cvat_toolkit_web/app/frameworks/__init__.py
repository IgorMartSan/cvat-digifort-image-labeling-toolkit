from app.frameworks.yolo import YoloTrainer, YoloPredictor

TRAINERS = {
    "yolo11": YoloTrainer(),
}

PREDICTORS = {
    "yolo11": YoloPredictor(),
}

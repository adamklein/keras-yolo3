wget https://pjreddie.com/media/files/yolov3.weights -P model_data
python convert.py yolov3.cfg model_data/yolov3.weights model_data/yolo_weights.h5

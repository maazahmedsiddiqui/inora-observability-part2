import requests
import time
import random

while True:
    requests.get('http://127.0.0.1:5000/catalog')
    requests.post('http://127.0.0.1:5000/order')
    
    if random.random() < 0.1:
        requests.post('http://127.0.0.1:5000/cancel')
        
    time.sleep(1)
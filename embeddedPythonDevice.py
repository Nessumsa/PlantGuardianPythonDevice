import RPi.GPIO as GPIO
import time
import socketio
import threading
import sys
import os
import queue
from RPLCD.i2c import CharLCD
import datetime
import json
import spidev

#Defining variables
PlantID = '0' #This is loaded from settingsfile
MoistureLimit = 0 #This is loaded from settingsfile
AllowedDryPeriod = 0 #This is loaded from settingsfile
RequiredWaterAmount = 0 #This is loaded from settingsfile
LastWatered = 0
WaterStatus = 0
NewInput = False
firstLoop = True
Metric = {}
InitializationDone = False #Bool used for checking initialization
SetupDone = False #Bool used for checking if connection is set up
Url = 'http://10.176.69.183:3000' #This should be sent via bluetooth
MessageQueue = queue.Queue()

#Defining messages
MessageStartUp = 'Welcome! Initialization starting now.'
MessageInitDone = 'Initialization done! Monitoring has begun.'
MessageConnectingSocketIO = 'Connected to server.'
MessageDisconnect = 'Disconnected from server.'
MessageLowWaterTank = 'The water tank is low, please fill it up.'
MessagePumpingWater = 'Watering plant.'
MessageImplementNewSettings = 'Implementing new settings. Please wait.'
MessageResetProgram = 'Restarting the application now.'
MessageShutdown = 'Shutting down.'

#PIN setup
RESET_BUTTON_PIN = 21
WATER_SENSOR_PIN = 16
PUMP_PIN = 24

#GPIO setup
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

# Initialization of output-pins
GPIO.setup(PUMP_PIN, GPIO.OUT)
GPIO.output(PUMP_PIN, GPIO.HIGH)

# Initialization of input-pins
GPIO.setup(RESET_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(WATER_SENSOR_PIN, GPIO.IN)

#Utility files
SETTINGS_FILE = "plantsettings.json"

#Hardware setup
client = socketio.Client()
lcd = CharLCD('PCF8574', 0x27)
spi = spidev.SpiDev()
spi.open(0,0)
spi.max_speed_hz = 1350000

#Function implementation
#SocketIO functions
@client.event
def connect():
    queue_message(message=MessageConnectingSocketIO, duration=2)
    client.emit('join_room', PlantID)

@client.event
def disconnect():
    queue_message(message=MessageDisconnect, duration=2)

@client.on('iot_config_new_data')
def on_new_data(config):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(config, f, indent=2)
    load_settings()

def start_socketio():
    client.connect(Url)
    client.wait()

def send_metric(metric):
    client.emit('send_metric', metric)

#Utility functions
def init_display():
    lcd.clear()

def scroll_message(message, duration, lcd, delay=0.3):
    if len(message) <= 32:
        lcd.clear()
        lcd.write_string(message[:16])  
        if len(message) > 16:
            lcd.cursor_pos = (1, 0)
            lcd.write_string(message[16:32])  
        time.sleep(duration)
    else:
        padded_msg = message + " " * 16
        total_steps = len(padded_msg) - 15
        start_time = time.time()

        while time.time() - start_time < duration:
            for i in range(total_steps):
                if time.time() - start_time >= duration:
                    break
                lcd.clear()
                lcd.write_string(padded_msg[i:i+16])
                time.sleep(delay)


def queue_handler():
    while not exit_event.is_set():
        if not MessageQueue.empty():
            message, duration = MessageQueue.get()
            scroll_message(message, float(duration), lcd)
            lcd.clear()
            MessageQueue.task_done()

def queue_message(message, duration):
    MessageQueue.put((message, duration))

def metric_creator(PlantId, TimeStamp, Moisture, LastWatered, WaterStatus):
    return {
        "roomId": PlantId,
        "entity": {
            "customPlantId": PlantID,    
            "timeStamp": TimeStamp,
            "moisture": Moisture,
            "lastWatered": LastWatered,
            "waterStatus": WaterStatus
        }
    }

def load_settings():
    with open(SETTINGS_FILE, "r") as f:
        settings = json.load(f)
    global PlantID; PlantID = settings["plantId"]
    global MoistureLimit; MoistureLimit = settings["moisture"]
    global AllowedDryPeriod; AllowedDryPeriod = settings["allowedDryPeriod"]
    global RequiredWaterAmount; RequiredWaterAmount = settings["requiredWaterAmount"]

def hard_reset_program():
    while True:
        if GPIO.input(RESET_BUTTON_PIN) == GPIO.LOW:
            StartTime = time.time()
            while GPIO.input(RESET_BUTTON_PIN) == GPIO.LOW:
                time.sleep(0.1)
                if time.time() - StartTime >= 3:
                    queue_message(message=MessageResetProgram, duration=3)
                    time.sleep(3)
                    shutdown(restart=True)
        time.sleep(0.1)  

def shutdown(restart=False):
    queue_message(message=MessageShutdown, duration=5)
    GPIO.cleanup()
    time.sleep(5)
    exit_event.set()  # Signal the queue_handler thread to exit
    lcd.clear()
    spi.close()
    lcd.close()
    time.sleep(2)
    if restart:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        sys.exit(0)

def clear_display():
    lcd.clear()

#Water tank functions
def measure_water_level():
    return GPIO.input(WATER_SENSOR_PIN)
    
def check_water_tank():
    if measure_water_level() < 1:
        global WaterStatus
        WaterStatus = 0
        queue_message(message=MessageLowWaterTank, duration=8)

def pump_on():
    GPIO.output(PUMP_PIN, GPIO.LOW)

def pump_off():
    GPIO.output(PUMP_PIN, GPIO.HIGH)

def pump_water(duration):
    pump_on()
    queue_message(message=MessagePumpingWater, duration=duration)
    time.sleep(duration)
    pump_off()
    check_water_tank() 

#Moisture functions
def read_channel(channel: int) -> int:
    if not 0 <= channel <= 7:
        raise ValueError("Channel must be 0-7")
    adc = spi.xfer2([1, (8 + channel) << 4, 0])
    data = ((adc[1] & 3) << 8) + adc[2]
    return data

def get_soil_moisture() -> int:
    return read_channel(0)

def measure_moisture() -> float:
    raw = get_soil_moisture()
    percent = 100 - ((raw / 1023) * 100)
    return round(percent, 1)

#Loop timing methods
def wait_until_next_run():
    now = datetime.datetime.now()
    today_8 = now.replace(hour=8, minute=0, second=0, microsecond=0)
    today_20 = now.replace(hour=20, minute=0, second=0, microsecond=0)

    if now < today_8:
        wait_time = (today_8 - now).total_seconds()
    elif now < today_20:
        wait_time = (today_20 - now).total_seconds()
    else:
        wait_time = ((today_8 + datetime.timedelta(days=1)) - now).total_seconds()

    time.sleep(wait_time)



#Threads setup
message_queue_thread = threading.Thread(target=queue_handler, daemon=True)
listener_thread = threading.Thread(target=start_socketio, daemon=True)
reset_thread = threading.Thread(target=hard_reset_program, daemon=True)
exit_event = threading.Event()

def initialization():
    global InitializationDone
    init_display()
    load_settings()
    message_queue_thread.start()
    reset_thread.start()
    listener_thread.start()
    queue_message(message=MessageStartUp, duration=3)
    InitializationDone = True
    
    
def loop():
    global MoistureLimit
    global PlantID
    global LastWatered
    global WaterStatus
    global firstLoop

    while True:
        if not firstLoop:
            wait_until_next_run()
        else:
            firstLoop = False

        try:
            MoistureMeasurement = measure_moisture()
            CurrentDate = datetime.datetime.now().isoformat()

            if MoistureMeasurement < MoistureLimit:
                pump_water(3)
                LastWatered = datetime.datetime.now().isoformat()
            else:
                check_water_tank()

            send_metric(metric_creator(PlantId=PlantID,TimeStamp=CurrentDate,Moisture=MoistureMeasurement,LastWatered=LastWatered,WaterStatus=WaterStatus))

        except Exception as e:
            print(e)
            client.send(f"Exception: {e}")


if __name__ == "__main__":
    if InitializationDone == False:
        initialization()
    loop()
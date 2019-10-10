"""
Example of using pycirculate with a simple Flask RESTful API.

Make sure to send requests with the HTTP header "Content-Type: application/json".

NOTE: Only a single BlueTooth connection can be open to the Anova at a time.  So
if you want to scale this API with multi-processing, keep that in mind to prevent errors
such as:
    `BTLEException: Failed to connect to peripheral 78:A5:04:38:B3:FA, addr type: public`
"""

#https://hackerthemes.com/kit/ -> One day adjust colors

#https://github.com/erikcw/pycirculate/blob/master/examples/rest/rest.py

from flask import Flask, request, jsonify, abort, make_response, render_template
from anova import AnovaController
from threading import Timer
import datetime
import pytz
import logging
import json
import os
import sys
import warnings
import json
import re
from apscheduler.schedulers.background import BackgroundScheduler
import enum 

class RESTAnovaController(AnovaController):
    """
    This version of the Anova Controller will keep a connection open over bluetooth
    until the timeout has been reach.

    NOTE: Only a single BlueTooth connection can be open to the Anova at a time.
    """

    TIMEOUT = 5 * 60 # Keep the connection open for this many seconds.
    TIMEOUT_HEARTBEAT = 20

    def __init__(self, mac_address, connect=True, logger=None):
        self.last_command_at = datetime.datetime.now()
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger()
        super(RESTAnovaController, self).__init__(mac_address, connect=connect)

    def set_timeout(self, timeout):
        """
        Adjust the timeout period (in seconds).
        """
        self.TIMEOUT = timeout

    def timeout(self, seconds=None):
        """
        Determines whether the Bluetooth connection should be timed out
        based on the timestamp of the last exectuted command.
        """
        if not seconds:
            seconds = self.TIMEOUT
        timeout_at = self.last_command_at + datetime.timedelta(seconds=seconds)
        if datetime.datetime.now() > timeout_at:
            self.close()
            self.logger.info('Timeout bluetooth connection. Last command ran at {0}'.format(self.last_command_at))
        else:
            self._timeout_timer = Timer(self.TIMEOUT_HEARTBEAT, lambda: self.timeout())
            self._timeout_timer.setDaemon(True)
            self._timeout_timer.start()
            self.logger.debug('Start connection timeout monitor. Will idle timeout in {0} seconds.'.format(
                (timeout_at - datetime.datetime.now()).total_seconds())) 

    def connect(self):
        super(RESTAnovaController, self).connect()
        self.last_command_at = datetime.datetime.now()
        self.timeout()

    def close(self):
        super(RESTAnovaController, self).close()
        try:
            self._timeout_timer.cancel()
        except AttributeError:
            pass

    def _send_command(self, command):
        if not self.is_connected:
            self.connect()
        self.last_command_at = datetime.datetime.now()
        return super(RESTAnovaController, self)._send_command(command)


app = Flask(__name__)
    

# initialize scheduler with your preferred timezone
# https://apscheduler.readthedocs.io/en/latest/userguide.html
# https://medium.com/@chetaniam/writing-a-simple-scheduling-service-with-apscheduler-dfbabc62e24a
scheduler = BackgroundScheduler({'apscheduler.timezone': 'America/New_York'})
scheduler.start()

#Track active jobs - format (number, job)
activeJobs = []
#activeJobs.append( (0, 'Dummy', 'No Start Time', 'Some Temperature', 'some Job') )

#Track Status to decrease lag time from site
anovaStatus = {'currentTemperature': 0.0,
               'setTemperature': 0.0,
               'deviceUnit': 'Unknown',
               'deviceStatus': 'Unknown',
              }


ANOVA_MAC_ADDRESS = "50:65:83:25:84:65"

# Error handlers
@app.errorhandler(400)
def bad_request(error):
    return make_response(jsonify({'error': 'Bad request.'}), 400)

@app.errorhandler(404)
def timeout_atnot_found(error):
    return make_response(jsonify({'error': 'Not found.'}), 404)

@app.errorhandler(500)
def server_error(error):
    return make_response(jsonify({'error': 'Server error.'}), 500)

def make_error(status_code, message, sub_code=None, action=None, **kwargs):
    """
    Error with custom message.
    """
    data = {
        'status': status_code,
        'message': message,
    }
    if action:
        data['action'] = action
    if sub_code:
        data['sub_code'] = sub_code
    data.update(kwargs)
    response = jsonify(data)
    response.status_code = status_code
    return response



#Keeps pinging the device and gets up to date info
def keepAliveStatus():
    try:
        anovaStatus['currentTemperature'] = float( app.anova_controller.read_temp() )
        anovaStatus['setTemperature'] = float( app.anova_controller.read_set_temp() )
        anovaStatus['deviceUnit'] = app.anova_controller.read_unit()
        anovaStatus['deviceStatus'] = app.anova_controller.anova_status()
    except:
        anovaStatus['currentTemperature'] = 'Unknown'
        anovaStatus['setTemperature'] = 'Unknown'
        anovaStatus['deviceUnit'] = 'Unknown'
        anovaStatus['deviceStatus'] = 'Unknown'
                                              
        
#Keep alive connection to Anova
#job = scheduler.add_job(AliveStatus, 'interval', seconds=10)


# REST endpoints

@app.route('/', methods=['GET', 'POST'])
@app.route('/home', methods=['GET', 'POST'])
def displayWebPage():
    return render_template('home.html')


@app.route('/get-temp', methods=["GET"])
def get_temp():
    try:
        output = {"current_temp": float(app.anova_controller.read_temp()), "set_temp": float(app.anova_controller.read_set_temp()), "unit": app.anova_controller.read_unit(),}
    except Exception as exc:
        app.logger.error(exc)
        return make_error(500, "{0}: {1}".format(repr(exc), str(exc)))

    return jsonify(output)


@app.route('/set-temp', methods=["POST"])
def set_temp():
    try:
        temp = request.get_json()['temp']
    except (KeyError, TypeError):
        abort(400)
    temp = float(temp)
    output = {"set_temp": float(app.anova_controller.set_temp(temp))}

    return jsonify(output)


@app.route('/get-status', methods=['GET'])
def set_status():
    output = {"devicestatus": app.anova_controller.anova_status()}
    return jsonify(output)


@app.route('/get-background-status', methods=['GET'])
def get_background_status():
    try:
        #Nothing to try - anovaStatus has up-to-date data
        pass
        
    except Exception as exc:
        app.logger.error(exc)
        return make_error(500, "{0}: {1}".format(repr(exc), str(exc)))

    return jsonify(anovaStatus)
    
    

@app.route('/stop', methods=["POST"])
def stop_anova():
    stop = app.anova_controller.stop_anova()
    if stop == "s":
        stop = "stopped"
    output = {"status": stop,}

    return jsonify(output)


@app.route('/start', methods=["POST"])
def start_anova():
    status = app.anova_controller.start_anova()
    if status == "s":
        status = "starting"
    output = {"status": status,}

    return jsonify(output)


@app.route('/set-timer', methods=["POST"])
def set_timer():
    try:
        minutes = request.get_json()['minutes']
    except (KeyError, TypeError):
        abort(400)
    output = {"set_minutes": int(app.anova_controller.set_timer(minutes)),}
    return jsonify(output)


@app.route('/start-timer', methods=["POST"])
def start_timer():
    # Anova must be running to start the timer.
    app.anova_controller.start_anova()
    output = {"timer_status": app.anova_controller.start_timer()}
    return jsonify(output)


@app.route('/stop-timer', methods=["POST"])
def stop_timer():
    output = {"timer_status": app.anova_controller.stop_timer()}
    return jsonify(output)


#Adding read-timer - needs to be tested
@app.route('/read-timer', methods=["POST", 'GET'])
def read_timer():
    output = {"timer_status": app.anova_controller.read_timer()}
    return jsonify(output)


@app.route('/set-timeout', methods=["POST"])
def set_timeout():
    """
    Adjust the Bluetooth connection timeout length.
    """
    try:
        seconds = int(request.get_json()['timeout_seconds'])
    except (KeyError, TypeError):
        abort(400)
    app.anova_controller.set_timeout(seconds)
    output = {"timeout_seconds": seconds,}
    return jsonify(output)


@app.route('/set-led', methods=['POST'])
def set_led():
    #Get led values from the front end
    rgbVals = request.get_json()
    
    print(type(rgbVals))
    
    app.anova_controller.set_led( int(rgbVals['rVal']),
                                  int(rgbVals['gVal']),
                                  int(rgbVals['bVal'])
                                )
    
    output = {"status":"success"}
    return jsonify(output)
    

@app.route('/set-delayed-start', methods=['POST'])
def process_actions():
    print('Process incoming action')
    try:
        #Get action from front end
        val = request.get_json()
        
        timeStart = val.get('timeStart')
        temperatureSet = val.get('temperatureSet')
        
        #Parse/Convert timeStart
        print(timeStart, temperatureSet)

        #Define different time zones
        eastern = pytz.timezone('US/Eastern')
        utc = pytz.utc

        #Convert string to UTC time
        test2 = datetime.datetime.strptime(str(timeStart), '%Y-%m-%dT%H:%M:%S.%fZ')   

        #Actually convert to UTC time
        utcTime = utc.localize(test2)
        print(utcTime)

        #Convert to EST
        estTime = utcTime.astimezone(pytz.timezone('US/Eastern'))
        print(estTime)

        #Convert to string
        estString = estTime.strftime("%m/%d/%Y, %H:%M:%S")

        #And finally back to datetime in EST
        startTime = datetime.datetime.strptime(estString, "%m/%d/%Y, %H:%M:%S")
        
        #Get next job id
        nextJobID = len(activeJobs)+1
        
        #Add start time
        job = scheduler.add_job(app.anova_controller.start_anova, trigger='date', next_run_time=str(startTime), id=str(nextJobID))
        activeJobs.append( (nextJobID, 'delayStart', str(startTime), temperatureSet, job) )
        
        #If temperature is given set it a second before start time
        if temperatureSet.strip() != '':
            tempTime = startTime + datetime.timedelta(seconds=1)
            
            #Get next job id
            nextJobID = len(activeJobs)+1
        
            #Create job
            job = scheduler.add_job(app.anova_controller.set_temp, trigger='date', next_run_time=str(tempTime), args=[temperatureSet], id=str(nextJobID))
        
            #Append tuple( int(jobNumber), str('delayStart'), str(timeStart), str(temperatureSet), job )
            activeJobs.append( (nextJobID, 'delayStart', str(startTime), temperatureSet, job) )
        
    except (KeyError, TypeError):
        abort(400)
        
    #Print active Flask jobs
        print(activeJobs)
    
    #Return job
    output = {"JobNumber": "Success"}
    return jsonify(output)
        

@app.route('/view-actions', methods=['GET'])
def view_action():
    toPass = []
    for (_jobNum, _jobType, _jobStartTime, _jobTemperature, _job) in activeJobs:
        toPass.append( {"jobNumber":_jobNum,
                        "jobType":_jobType,
                        "jobStartTime":_jobStartTime,
                        "jobTemperature":_jobTemperature,
                        "job":'job'}
                     )
    #Print active Flask Jobs
    print(activeJobs)
        
    return jsonify(toPass)

    
@app.route('/delete-actions', methods=['POST'])
def delete_action():
    
    try:
        #Get JSON
        val = request.get_json()
        
        #Get action ID
        webActionID = int(val.get('actionID').strip('actionID_'))
        
        print('Web action ID to delete: {}'.format(webActionID) )

        for i in range(len(activeJobs)):
            if activeJobs[i][0] == webActionID:
                print('Scheduled ID to delete: {}'.format(webActionID-1) )
                scheduler.remove_job( str(activeJobs[i][0]) )
                print('activeJob to delete: {}'.format(i) )
                del activeJobs[i]
                break

        toReturn = {"status":"deleted"}
        
    except (KeyError, TypeError):
        abort(400)
    
    print('Done with deleting')
    return jsonify(toReturn)



try:
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    app.logger.addHandler(handler)

    print('Start Anova')
    app.anova_controller = RESTAnovaController(ANOVA_MAC_ADDRESS, logger=app.logger)
    print(app.anova_controller.read_temp())

    print('Start Flask')
    #Run with debug off - or you'll get errors!
    #app.run(host='192.168.1.101', port=5000, debug=False)
    app.run()


except BaseException as e:
    print(e)

finally:
    app.anova_controller.close()



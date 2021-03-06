import subprocess
import os
import time
import core
import config
import rss
from datetime import datetime, timedelta
from core import Recording

# Schedule a pass job
def schedulePass(pass_to_add, satellite, custom_aos = 0, custom_los = 0):

    # Allow setting custom aos/los
    if custom_aos == 0:
        custom_aos = pass_to_add.aos
    if custom_los == 0:
        custom_los = pass_to_add.los

    # Schedule the task
    core.scheduler.add_job(recordPass, 'date', [satellite, custom_los], run_date=custom_aos)
    print("Scheduled " + satellite.name + " pass at " + str(custom_aos))

# Schedule passes and resolve conflicts
def updatePass():
    passes = list()
    timenow = datetime.utcnow()

    # Lookup next passes of all satellites
    for satellite in config.satellites:
        predictor = satellite.getPredictor()
        next_pass = predictor.get_next_pass(config.location, max_elevation_gt=satellite.min_elevation)
        max_elevation = next_pass.max_elevation_deg
        priority = satellite.priority

        # Filter those coming in the next hour
        if next_pass.aos < timenow + timedelta(hours=1):
            passes.append([next_pass, satellite, max_elevation, priority])

    # Solve conflicts, a conflict being 2 satellites over horizon at the same time
    for current_pass in passes:
        current_pass_obj = current_pass[0]
        current_sat_obj = current_pass[1]
        current_max_ele = current_pass[2]
        current_priority = current_pass[3]

        keep = True
        keep_modified = False
        custom_aos = 0
        custom_los = 0
        for next_pass, satellite, max_elevation, priority in passes:
            # Skip if this is the same
            if next_pass == current_pass_obj:
                continue

            # Test if those 2 conflicts
            if next_pass.aos <= current_pass_obj.los and not next_pass.los <= current_pass_obj.aos:
                # If the priority is the same, chose the best pass
                if current_priority == priority:
                    if current_max_ele < max_elevation:
                        keep = False

                        # Schedule the pass if it doesn't overlap too much
                        overlapping_time = current_pass_obj.los - next_pass.aos
                        if overlapping_time < timedelta(minutes=config.maximum_overlap):
                            keep_modified = True
                            custom_aos = current_pass_obj.aos
                            custom_los = next_pass.aos
                else:
                    # Always prefer higher priorities
                    if current_priority < priority:
                        keep = False

                        # Schedule the pass if it doesn't overlap too much
                        overlapping_time = current_pass_obj.los - next_pass.aos
                        if overlapping_time < timedelta(minutes=config.maximum_overlap):
                            keep_modified = True
                            custom_aos = current_pass_obj.aos
                            custom_los = next_pass.aos

        # Schedule the task
        if keep:
            schedulePass(current_pass_obj, current_sat_obj)
        elif keep_modified:
            schedulePass(current_pass_obj, current_sat_obj, custom_aos=custom_aos, custom_los=custom_los)

# APT Pass record function
def recordAPT(satellite, end_time):
    print("AOS " + satellite.name + "...")
    date = datetime.utcnow()

    # Build filename
    filename = config.output_dir + "/" + satellite.name + "/" + satellite.name + " at " + str(datetime.utcnow())
    print("Saving as '" + filename + "'")

    # Build command. We receive with rtl_fm and output a .wav with ffmpeg
    command = "rtl_fm -f " + str(satellite.frequency) + "M -M mbfm -s 60000 -r 48000 - | ffmpeg -f s16le -channels 1 -sample_rate 48k -i pipe:0 -f wav '" + filename + ".wav'"
    subprocess.Popen([command], shell=1)

    # Wait until pass is over
    while end_time >= datetime.utcnow():
        time.sleep(1)
    
    # End our command
    subprocess.Popen("killall rtl_fm".split(" "))

    print("LOS " + satellite.name + "...")

    # Give it some time to exit and queue the decoding
    time.sleep(10)
    core.decoding_queue.append(Recording(satellite, filename, date))

# LRPT Pass record function
def recordLRPT(satellite, end_time):
    print("AOS " + satellite.name + "...")
    date = datetime.utcnow()

    # Build filename
    filename = config.output_dir + "/" + satellite.name + "/" + satellite.name + " at " + str(datetime.utcnow())
    print("Saving as '" + filename + "'")

    # Build command. We receive with rtl_fm and output a raw output to feed into the demodulator
    command = "rtl_fm -M raw -s 140000 -f " + str(satellite.frequency) + "M -E dc '" + filename + ".raw'"
    subprocess.Popen([command], shell=1)

    # Wait until pass is over
    while end_time >= datetime.utcnow():
        time.sleep(1)
    
    # End our command
    subprocess.Popen("killall rtl_fm".split(" "))

    print("LOS " + satellite.name + "...")

    # Give it some time to exit and queue the decoding
    time.sleep(10)
    core.decoding_queue.append(Recording(satellite, filename, date))

# Downlink mode redirection
def recordPass(satellite, end_time):
    # Lock the radio to prevent any issues
    core.radio_lock.acquire()

    # Record the pass!
    if satellite.downlink == "APT":
        recordAPT(satellite, end_time)
    elif satellite.downlink == "LRPT":
        recordLRPT(satellite, end_time)

    # Release the radio
    core.radio_lock.release()

# Decode APT file
def decodeAPT(filename, delete_processed_files):
    output_files = list()
    print("Decoding '" + filename + "'...")

    # Build noaa-apt command
    command = "noaa-apt '" + filename + ".wav' -o '" + filename + ".png'"

    # Run and delete the recording to save disk space
    if subprocess.Popen([command], shell=1).wait() == 0 and delete_processed_files:
        os.remove(filename + ".wav")
    
    # Return a list of produced outputs
    output_files.append(filename + ".png")

    print("Done decoding'" + filename + "'!")

    return output_files

# Decode LRPT file
def decodeLRPT(filename, delete_processed_files):
    output_files = list()
    print("Demodulating '" + filename + "'...")

    # Demodulate with meteor_demod
    command = "meteor_demod -B -s 140000 '" + filename + ".raw' -o '" + filename + ".lrpt'"
    if subprocess.Popen([command], shell=1).wait() == 0 and delete_processed_files:
        os.remove(filename + ".raw")
    
    print("Decoding '" + filename + "'...")

    # Decode with meteor_decoder. Both IR & Visible
    command1 = "medet '" + filename + ".lrpt' '" + filename + " - Visible' -r 65 -g 65 -b 64"
    command2 = "medet '" + filename + ".lrpt' '" + filename + " - Infrared' -r 68 -g 68 -b 68"
    process2 = subprocess.Popen([command2], shell=1)
    if subprocess.Popen([command1], shell=1).wait() == 0 and process2.wait() == 0 and delete_processed_files:
        os.remove(filename + ".lrpt")
    
    # Convert to png to save on space
    command1 = "ffmpeg -i '" + filename + " - Visible.bmp' '" + filename + " - Visible.png' "
    command2 = "ffmpeg -i '" + filename + " - Infrared.bmp' '" + filename + " - Infrared.png' "
    if subprocess.Popen([command1], shell=1).wait() == 0 and subprocess.Popen([command2], shell=1).wait() == 0 and delete_processed_files:
        os.remove(filename + " - Visible.bmp")
        os.remove(filename + " - Infrared.bmp")

    # Return a list of produced outputs
    output_files.append(filename + " - Visible.bmp")
    output_files.append(filename + " - Infrared.bmp")

    print("Done decoding'" + filename + "'!")

    return output_files

# Redirect to the right decoder function
def decodePass(filename, satellite, date):
    output_files = list()
    if satellite.downlink == "APT":
        output_files = decodeAPT(filename, satellite.delete_processed_files)
    elif satellite.downlink == "LRPT":
        output_files = decodeLRPT(filename, satellite.delete_processed_files)
    else:
        return

    # Add on the RSS feed if enabled
    if config.rss_enabled:
        rss.addRSSPass(satellite, filename.replace(config.output_dir + "/", ""), date)

    # Process post-processing hook if enabled
    if config.post_processing_hook_enabled:
        if config.post_processing_hook_foreach:
            for file_out in output_files:
                command = config.post_processing_hook_command.replace("{file}", "'" + file_out + "'")
                subprocess.Popen([command], shell=1).wait()
        else:
            file_list = str()
            for file_out in output_files:
                file_list += "'" + file_out + "' "
            command = config.post_processing_hook_command.replace("{file}", file_list)
            subprocess.Popen([command], shell=1).wait()

# Process pending decodings
def processDecodeQueue():
    while True:
        time.sleep(1)
        if len(core.decoding_queue) > 0:
            decode = core.decoding_queue[0]
            decodePass(decode.filename, decode.satellite, decode.date)
            core.decoding_queue.remove(decode)
            
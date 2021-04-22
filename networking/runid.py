"""Dextr - task managing for PyDex
Stefan Spence 11/10/19

 - control the run number ID for experimental runs
 - emit signals between modules when images are taken
 - keep other modules synchronised
"""
import time
import os
import numpy as np
try:
    from PyQt4.QtCore import QThread, pyqtSignal, QTimer
    from PyQt4.QtGui import QMessageBox
except ImportError:
    from PyQt5.QtCore import QThread, pyqtSignal, QTimer
    from PyQt5.QtWidgets import QMessageBox
from networker import PyServer, reset_slot, TCPENUM
import sys
if '..' not in sys.path: sys.path.append('..')
from strtypes import error, warning, info

class runnum(QThread):
    """Take ownership of the run number that is
    synchronised between modules of PyDex.
    By running this on a separated thread the 
    main GUI should not freeze up. PyQt signal/slot
    architecture has a queue in the eventloop to 
    ensure function requests are not missed.
    keyword arguments:
    camra - an instance of ancam.cameraHandler.camera
    saver - an instance of savim.imsaver.event_handler
    saiaw - an instance of settingsgui.settings_window
    check - an instance of atomChecker.atom_window
    seq   - an instance of sequencePreviewer.Previewer
    n     - the initial run ID number
    m     - the number of images taken per sequence
    k     - the number of images taken already"""
    im_save = pyqtSignal(np.ndarray) # send an incoming image to saver
    Dxstate = 'unknown' # current state of DExTer

    def __init__(self, camra, saver, saiaw, check, seq, n=0, m=1, k=0):
        super().__init__()
        self._n = n # the run number
        self._m = m # # images per run
        self._k = k # # images received
        self.multirun = False # status of whether in multirun or not
        self.rearranging = False # whether the first image is being used for rearrangement.
        self.cam = camra # Andor camera control
        self.cam.AcquireEnd.connect(self.receive) # receive the most recent image
        self.sv = saver  # image saver
        self.im_save.connect(self.sv.add_item) # separate signal to avoid risk of the slot being disconnected elsewhere
        self.sv.start()  # constantly checks queue, when an image to save is added to the queue, it saves it to a file.
        self.sw = saiaw  # image analysis settings gui
        self.sw.m_changed.connect(self.set_m)
        self.sw.CCD_stat_edit(self.cam.emg, self.cam.pag, self.cam.Nr, True) # give image analysis the camera settings
        self.sw.reset_analyses() # make sure the loaded config settings are applied
        self.cam.SettingsChanged.connect(self.sw.CCD_stat_edit)
        self.cam.ROIChanged.connect(self.sw.cam_pic_size_changed) # triggers pic_size_text_edit()
        self.check = check  # atom checker for ROIs, trigger experiment
        self.check.nrois_edit.setText(str(len(self.sw.stats['ROIs'])))
        self.cam.ROIChanged.connect(self.check.rh.cam_pic_size_changed)
        self.check.recv_rois_action.triggered.connect(self.get_rois_from_analysis)
        self.get_rois_from_analysis()
        self.sw.bias_changed.connect(self.check.rh.set_bias)
        self.check.roi_values.connect(self.sw.set_rois)
        self.seq = seq   # sequence editor
        
        self.server = PyServer(host='', port=8620) # server will run continuously on a thread
        self.server.dxnum.connect(self.set_n) # signal gives run number
        self.server.start()
        if self.server.isRunning():
            self.server.add_message(TCPENUM['TCP read'], 'Sync DExTer run number\n'+'0'*2000) 

        self.trigger = PyServer(host='', port=8621) # software trigger using TCP
        self.trigger.start()
        self.monitor = PyServer(host='', port=8622) # monitor program runs separately
        self.monitor.start()
        self.monitor.add_message(self._n, 'resync run number')
        self.awgtcp = PyServer(host='', port=8623) # AWG program runs separately
        self.awgtcp.start()
        self.ddstcp = PyServer(host='', port=8624) # DDS program runs separately
        self.ddstcp.start()
        self.seqtcp = PyServer(host='', port=8625) # Sequence viewer in seperate instance of LabVIEW
        self.seqtcp.start()
            
    def reset_server(self, force=False):
        """Check if the server is running. If it is, don't do anything, unless 
        force=True, then stop and restart the server. If the server isn't 
        running, then start it."""
        for server in [self.server, self.trigger, self.monitor, self.awgtcp, self.ddstcp, self.seqtcp]:
            if server.isRunning():
                if force:
                    server.close()
                    server.clear_queue()
                    time.sleep(0.1) # give time for it to close
                    server.start()
            else: server.start()
            
    def set_n(self, dxn):
        """Change the Dexter run number to the new value.
        If it's during a multirun, check that the right number of 
        images were taken in the last run."""
        self._n = int(dxn)
    
    def set_m(self, newm):
        """Change the number of images per run"""
        if newm > 0:
            self._m = int(newm)
        elif newm == 0:
            self._m = 2
        if self.rearranging: self._m += 1

    def receive(self, im=0):
        """Update the Dexter file number in all associated modules,
        then send the image array to be saved and analysed."""
        self.sv.dfn = str(self._n) # Dexter file number
        imn = self._k % self._m # ID number of image in sequence
        if self.rearranging: imn -= 1 # for rearranging, the 1st image doesn't go to analysis
        self.sv.imn = str(imn) 
        self.im_save.emit(im)
        if imn < 0:
            self.check.event_im.emit(im)
        else:
            for i in self.sw.find(imn): # find the histograms that use this image
                self.sw.mw[i].image_handler.fid = self._n
                self.sw.mw[i].event_im.emit(im, self._k < self._m and not self.check.checking)
        self._k += 1 # another image was taken

    def unsync_receive(self, im=0):
        """Receive an image array to be saved and analysed.
        Count the number of images taken and use this to set the
        DExTer file number in all associated modules."""
        self.receive(im)
        if self._k % self._m == 0:
            self._k = 0
            self.server.dxnum.emit(str(self._n + 1)) 
        
    def mr_receive(self, im=0):
        """Receive an image as part of a multirun.
        Update the Dexter file number in all associated modules,
        then send the image array to be saved and analysed."""
        self.sv.dfn = str(self._n) # Dexter file number
        imn = self._k % self._m # ID number of image in sequence
        if self.rearranging: imn -= 1 # for rearranging, the 1st image doesn't go to analysis
        self.sv.imn = str(imn) 
        self.im_save.emit(im)
        if imn < 0:
            self.check.event_im.emit(im)
        else:
            if self.seq.mr.ind % (self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']) >= self.seq.mr.mr_param['# omitted']:
                for i in self.sw.find(imn):
                    self.sw.mw[i].image_handler.fid = self._n
                    self.sw.mw[i].event_im.emit(im, self._k < self._m and not self.check.checking)
        self._k += 1 # another image was taken

    def check_receive(self, im=0):
        """Receive image for atom checker, don't save but just pass on"""
        self.check.event_im.emit(im)

    def reset_dates(self, t0):
        """Make sure that the dates in the image saving and analysis 
        programs are correct."""
        date = time.strftime("%d %b %B %Y", t0).split(" ")
        self.sv.reset_dates(self.sv.config_fn, date=date)
        self.sw.reset_dates(date)
        return ' '.join([date[0]] + date[2:])
    
    #### atom checker ####

    def get_rois_from_analysis(self):
        self.check.rh.cam_pic_size_changed(self.sw.stats['pic_width'], self.sw.stats['pic_height'])
        self.check.rh.resize_rois(self.sw.stats['ROIs'])

    def send_rearr_msg(self, msg=''):
        """Send the command to the AWG for rearranging traps"""
        self.awgtcp.priority_messages([(self._n, msg)])

    def atomcheck_go(self, toggle=True):
        """Disconnect camera images from analysis, start the camera
        acquisition and redirect the images to the atom checker."""
        if self.cam.initialised > 1:
            self.check.checking = True
            self.trigger.start() # start server for TCP to send msg when atoms loaded
            # redirect images from analysis to atom checker
            reset_slot(self.cam.AcquireEnd, self.receive, False)
            reset_slot(self.cam.AcquireEnd, self.mr_receive, False)
            reset_slot(self.cam.AcquireEnd, self.check_receive, True)
            # still in external exposure trigger - DExTer will send the trigger pulses
            self.cam.start() # run till abort keeps taking images
            if self.check.timer.t0 > 0: # if timeout is set, set a timer
                self.check.timer.singleShot(int(self.check.timer.t0*1e3), self.check.send_trigger)

    #### multirun ####

    def get_params(self, v, module='AWG'):
        """Reformat the multirun paramaters into a string to be sent to the AWG or DDS"""
        msg = module+' set_data=['
        col = -1  # in case the for loop doesn't execute
        for col in range(len(self.seq.mr.mr_param['Type'])):
            if 'AWG' in self.seq.mr.mr_param['Type'][col] and module == 'AWG':
                try: # argument: value
                    n = self.seq.mr.mr_param['Time step name'][col][0] # index of chosen AWG channel, segment 
                    msg += '[%s, %s, "%s", %s, %s],'%(n%4, n//4, 
                        self.seq.mr.awg_args[self.seq.mr.mr_param['Analogue channel'][col][0]], 
                        self.seq.mr.mr_vals[v][col], self.seq.mr.mr_param['list index'][col])
                except Exception as e: error('Invalid AWG parameter at (%s, %s)\n'%(v,col)+str(e))
            elif 'DDS' in self.seq.mr.mr_param['Type'][col] and module == 'DDS':
                try: # argument: value
                    n = self.seq.mr.mr_param['Time step name'][col][0] # index of chosen DDS COM port, profile
                    msg += '["COM%s", "P%s", "%s", %s],'%((n//8)+7, n%8, 
                        self.seq.mr.dds_args[self.seq.mr.mr_param['Analogue channel'][col][0]], 
                        self.seq.mr.mr_vals[v][col])
                except Exception as e: error('Invalid DDS parameter at (%s, %s)\n'%(v,col)+str(e))
        if col > -1: msg = msg[:-1] + ']'
        else: msg += ']'
        return msg
    
    def multirun_go(self, toggle, stillrunning=False):
        """Initiate the multi-run: omit N files, save a histogram of M files, and
        repeat for the user variables in the list. A new sequence is generated for 
        each multirun run. These are sent via TCP and then run. Once the multirun
        has started it"""
        r = self.seq.mr.ind % (self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']) # ID of run in repetition cycle
        if toggle: # and self.sw.check_reset() < now will auto reset (so you can queue up multiruns)
            try: # take the multirun parameters from the queue (they're added to the queue in master.py)
                self.seq.mr.mr_param, self.seq.mr.mrtr, self.seq.mr.mr_vals, self.seq.mr.appending = self.seq.mr.mr_queue.pop(0) # parameters, sequence, values, whether to append
            except IndexError as e:
                error('runid.py could not start multirun because no multirun was queued.\n'+str(e))
                return 0
                
            results_path = os.path.join(self.sv.results_path, self.seq.mr.mr_param['measure_prefix'])
            reset_slot(self.cam.AcquireEnd, self.receive, False) # only receive if not in '# omit'
            reset_slot(self.cam.AcquireEnd, self.mr_receive, True)
            self.seq.mr.ind = 0 # counter for how far through the multirun we are
            self._k = 0 # reset image per run count
            try:
                uv = self.seq.mr.mr_vals[0][0]
            except IndexError: uv = 'IndexError'
            self.seq.mr.progress.emit(       # update progress label
                'multirun measure %s: %s: %s, omit %s of %s files, %s of %s histogram files, 0%% complete'%(
                    self.seq.mr.mr_param['measure'], self.seq.mr.mr_param['Variable label'], 
                    uv, 0, self.seq.mr.mr_param['# omitted'], 0, self.seq.mr.mr_param['# in hist']))
            # make the directories
            try:
                os.makedirs(results_path, exist_ok=True)
                os.makedirs(os.path.join(results_path, 'sequences'), exist_ok=True)
                # save sequences and make list of messages to send and the order:
                self.seq.mr.mrtr.write_to_file(os.path.join(results_path, 'sequences', self.seq.mr.mr_param['measure_prefix'] + '_base.xml'))
                self.seq.mr.get_all_sequences(save_dir=os.path.join(results_path, 'sequences'))
                self.seq.mr.save_mr_params(os.path.join(results_path, self.seq.mr.mr_param['measure_prefix'] + 
                    'params' + str(self.seq.mr.mr_param['1st hist ID']) + '.csv'))
                self.sw.init_analysers_multirun(results_path, str(self.seq.mr.mr_param['measure_prefix']), self.seq.mr.appending)
            except FileNotFoundError as e:
                error('Multirun could not start because of invalid directory %s\n'%results_path+str(e))
                return 0
            # tell the monitor program to save results to the new directory
            self.monitor.add_message(self._n, results_path+'=save_dir')
            self.monitor.add_message(self._n, 'start')
            # insert TCP messages at the front of the queue: once the multirun starts don't interrupt it.
            repeats = self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']
            # list of TCP messages for the whole multirun
            mr_queue = [[TCPENUM['TCP read'], 'AWG save='+os.path.join(results_path,'AWGparam'+str(self.seq.mr.mr_param['1st hist ID'])+'.txt')+'||||||||'+'0'*2000],
                [TCPENUM['TCP read'], 'DDS save_STP='+os.path.join(results_path,'DDSparam'+str(self.seq.mr.mr_param['1st hist ID'])+'.txt')+'||||||||'+'0'*2000],] 
            for v in range(len(self.seq.mr.mr_vals)): # use different last time step during multirun
                if any('AWG' in x for x in self.seq.mr.mr_param['Type']): # send AWG parameters by TCP
                    awgmsg = self.get_params(v, 'AWG')
                else: awgmsg = ''
                if any('DDS' in x for x in self.seq.mr.mr_param['Type']): # send DDS parameters by TCP
                    ddsmsg = self.get_params(v, 'DDS')
                else: ddsmsg = ''
                mr_queue += [[TCPENUM['TCP read'], awgmsg+'||||||||'+'0'*2000], # set AWG parameters
                    [TCPENUM['TCP read'], ddsmsg+'||||||||'+'0'*2000], # set AWG parameters
                    [TCPENUM['TCP load last time step'], self.seq.mr.mr_param['Last time step run']+'0'*2000],
                    [TCPENUM['TCP load sequence from string'], self.seq.mr.msglist[v]]] + [
                    [TCPENUM['Run sequence'], 'multirun run '+str(self._n + r + repeats*v)+'\n'+'0'*2000] for r in range(repeats)
                    ] + [[TCPENUM['TCP read'], 'save and reset histogram\n'+'0'*2000]]
            # reset last time step for the last run:
            mr_queue.insert(len(mr_queue) - 2, [TCPENUM['TCP load last time step'], self.seq.mr.mr_param['Last time step end']+'0'*2000])
            mr_queue += [[TCPENUM['TCP read'], 'confirm last multirun run\n'+'0'*2000], 
                [TCPENUM['TCP read'], 'end multirun '+str(self.seq.mr.mr_param['measure'])+'\n'+'0'*2000]]
            for enum, text in mr_queue:
                self.server.add_message(enum, text)
            self.seq.mr.mr_param['runs included'][0].append(self._n) # keep track of which runs are in the multirun.
        else: # pause the multi-run
            reset_slot(self.cam.AcquireEnd, self.mr_receive, False)
            reset_slot(self.cam.AcquireEnd, self.receive, True) # process every image
            self.server.clear_queue()
            if any('AWG' in x for x in self.seq.mr.mr_param['Type']):
                self.awgtcp.add_message(self._n, 'AWG load='+os.path.join(self.sv.results_path, # reset AWG parameters
                    self.seq.mr.mr_param['measure_prefix'],'AWGparam'+str(self.seq.mr.mr_param['1st hist ID'])+'.txt'))
                self.awgtcp.add_message(self._n, 'AWG start_awg') # keep AWG on even after multirun
            self.cam.AF.AbortAcquisition()
            self.multirun = stillrunning
            if not stillrunning: 
                self.seq.mr.ind = 0
                self._k = 0
                for mw in self.sw.mw + self.sw.rw:
                    mw.multirun = ''
            status = ' paused.' if stillrunning else ' ended.'
            text = 'STOPPED. Multirun measure %s: %s is'%(self.seq.mr.mr_param['measure'], self.seq.mr.mr_param['Variable label'])
            self.seq.mr.progress.emit(text+status)
            self.server.add_message(TCPENUM['Run sequence'], text+status) # a final run, needed to trigger the AWG to start.

    def multirun_resume(self, status):
        """Resume the multi-run where it was left off.
        If the multirun is already running, do nothing."""
        if 'paused' in status: 
            reset_slot(self.cam.AcquireEnd, self.receive, False) # only receive if not in '# omit'
            reset_slot(self.cam.AcquireEnd, self.mr_receive, True)
            self._k = 0 # reset image per run count
            repeats = self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']
            r = self.seq.mr.ind % repeats  # repeat
            v = self.seq.mr.ind // repeats # variable
            nrows = len(self.seq.mr.mr_vals)
            if v > nrows - 1: v = nrows - 1
            # finish this histogram
            mr_queue = [[TCPENUM['TCP read'], 'AWG save='+os.path.join(
                        self.sv.results_path, self.seq.mr.mr_param['measure_prefix'],'AWGparam'+str(self.seq.mr.mr_param['1st hist ID'])+'.txt'
                        ) + '||||||||'+'0'*2000 if any('AWG' in x for x in self.seq.mr.mr_param['Type']) else '||||||||'+'0'*2000],
                [TCPENUM['TCP read'], 'restart measure %s'%(self.seq.mr.mr_param['measure'])+'\n'+'0'*2000],
                [TCPENUM['TCP load last time step'], self.seq.mr.mr_param['Last time step run']+'0'*2000],
                [TCPENUM['TCP load sequence from string'], self.seq.mr.msglist[v]]]
            mr_queue += [[TCPENUM['Run sequence'], 'multirun run '+str(self._n + i)+'\n'+'0'*2000] for i in range(repeats - r + 1)
                ] + [[TCPENUM['TCP read'], 'save and reset histogram\n'+'0'*2000]]
            for var in range(v+1, nrows): # add the rest of the multirun
                if any('AWG' in x for x in self.seq.mr.mr_param['Type']): # send AWG parameters by TCP
                    awgmsg = self.get_params(var)
                else: awgmsg = ''
                mr_queue += [[TCPENUM['TCP read'], awgmsg+'||||||||'+'0'*2000], # set AWG parameters
                    [TCPENUM['TCP load sequence from string'], self.seq.mr.msglist[var]]] + [
                    [TCPENUM['Run sequence'], 'multirun run '+str(self._n + r + repeats*var)+'\n'+'0'*2000] for r in range(repeats)
                    ] + [[TCPENUM['TCP read'], 'save and reset histogram\n'+'0'*2000]]
            mr_queue.insert(len(mr_queue) - 2, [TCPENUM['TCP load last time step'], self.seq.mr.mr_param['Last time step end']+'0'*2000])
            mr_queue += [[TCPENUM['TCP read'], 'confirm last multirun run\n'+'0'*2000], 
                [TCPENUM['TCP read'], 'end multirun '+str(self.seq.mr.mr_param['measure'])+'\n'+'0'*2000]]
            self.server.priority_messages(mr_queue) # adds at front of queue
            
    def multirun_step(self, msg):
        """Update the status label for the multirun
        The data for the run is received and processed when the command for the 
        next run is being sent, so the histogram is saved, fitted, and reset
        based on the run number +1."""
        self.monitor.add_message(self._n, 'update run number')
        if self._k != self._m - int(self.rearranging) and self.seq.mr.ind > 1:
            warning('Run %s took %s / %s images.'%(self._n, self._k, self._m))
        self._k = 0
        r = self.seq.mr.ind % (self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']) # repeat
        if r == 1:
            self.monitor.add_message(self._n, 'set fadelines') # keep the trace from the start of the histogram
        v = self.seq.mr.get_next_index(self.seq.mr.ind) # variable
        try:
            # if r >= self.seq.mr.mr_param['# omitted']: 
            #     self.seq.mr.mr_param['runs included'][v].append(self._n) # include this run in the multirun
            uv = self.seq.mr.mr_vals[v][0] # get user variable 
        except IndexError: 
            if v == len(self.seq.mr.mr_vals):
                uv = self.seq.mr.mr_vals[v-1][0] # get user variable 
            else: uv = 'IndexError'
        self.seq.mr.ind += 1
        r = self.seq.mr.ind % (self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist'])
        self.seq.mr.progress.emit(       # update progress label
            'multirun measure %s: %s: %s, omit %s of %s files, %s of %s histogram files, %.3g %% complete'%(
                self.seq.mr.mr_param['measure'], self.seq.mr.mr_param['Variable label'], uv, 
                r if r < self.seq.mr.mr_param['# omitted'] else self.seq.mr.mr_param['# omitted'], self.seq.mr.mr_param['# omitted'], 
                r - self.seq.mr.mr_param['# omitted'] if r > self.seq.mr.mr_param['# omitted'] else 0, self.seq.mr.mr_param['# in hist'], 
                self.seq.mr.ind / (self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']) / len(self.seq.mr.mr_vals)*100))
                
    def multirun_save(self, msg):
        """end of histogram: fit, save, and reset --- check this doesn't miss an image if there's lag"""
        v = self.seq.mr.ind // (self.seq.mr.mr_param['# omitted'] + self.seq.mr.mr_param['# in hist']) - 1 # previous variable
        try:
            prv = self.seq.mr.mr_vals[v][0] # get user variable from the previous row
        except AttributeError as e:     
            error('Multirun step could not extract user variable from table at row %s.\n'%v+str(e))
            prv = ''
        # fit and save data
        self.sw.multirun_save(self.sv.results_path, 
            self.seq.mr.mr_param['measure_prefix'], 
            self._n, prv, str(v+self.seq.mr.mr_param['1st hist ID']))
        
    def multirun_end(self, msg):
        """At the end of the multirun, save the plot data and reset"""
        self.monitor.add_message(self._n, 'DAQtrace.csv=trace_file')
        self.monitor.add_message(self._n, 'save trace') # get the monitor to save the last acquired trace
        self.sw.end_multirun() # reconnect signals and display empty hist
        self.monitor.add_message(self._n, 'save graph') # get the monitor to save the graph  
        self.monitor.add_message(self._n, 'stop') # stop monitoring
        self.multirun_go(False) # reconnect signals
        self.seq.mr.ind = 0
        # save over log file with the parameters used for this multirun (now including run numbers):
        self.seq.mr.save_mr_params(os.path.join(self.sv.results_path, os.path.join(self.seq.mr.mr_param['measure_prefix'],
            self.seq.mr.mr_param['measure_prefix'] + 'params' + str(self.seq.mr.mr_param['1st hist ID']) + '.csv')))
        self.seq.mr.progress.emit(       # update progress label
            'Finished measure %s: %s.'%(self.seq.mr.mr_param['measure'], self.seq.mr.mr_param['Variable label']))
        self.multirun = False
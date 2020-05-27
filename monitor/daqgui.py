"""PyDex Monitoring
Stefan Spence 27/05/20

A GUI displays the data as it's acquired, and collects statistics
from multiple traces.
More information on the DAQ is available at:
https://documentation.help/NI-DAQmx-Key-Concepts/documentation.pdf
We use the python module: https://nidaqmx-python.readthedocs.io
"""
import os
import re
import sys
import time 
import numpy as np
import pyqtgraph as pg
pg.setConfigOption('background', 'w') # set graph background default white
pg.setConfigOption('foreground', 'k') # set graph foreground default black        
from collections import OrderedDict
# some python packages use PyQt4, some use PyQt5...
try:
    from PyQt4.QtCore import pyqtSignal, QRegExp
    from PyQt4.QtGui import (QApplication, QPushButton, QWidget, QLabel, QAction,
            QGridLayout, QMainWindow, QMessageBox, QLineEdit, QIcon, QFileDialog,
            QMenu, QActionGroup, QFont, QTableWidget, QTableWidgetItem, QTabWidget, 
            QVBoxLayout, QDoubleValidator, QIntValidator, QRegExpValidator, 
            QComboBox) 
except ImportError:
    from PyQt5.QtCore import pyqtSignal, QRegExp
    from PyQt5.QtGui import (QIcon, QDoubleValidator, QIntValidator, QFont,
        QRegExpValidator)
    from PyQt5.QtWidgets import (QActionGroup, QVBoxLayout, QMenu, 
        QFileDialog, QMessageBox, QLineEdit, QGridLayout, QWidget,
        QApplication, QPushButton, QAction, QMainWindow, QTabWidget,
        QTableWidget, QTableWidgetItem, QLabel, QComboBox)
import logging
logger = logging.getLogger(__name__)
sys.path.append('.')
sys.path.append('..')
from strtypes import strlist, BOOL
from daqController import worker, remove_slot

double_validator = QDoubleValidator() # floats
int_validator    = QIntValidator()    # integers
int_validator.setBottom(0) # don't allow -ve numbers
bool_validator = QIntValidator(0,1)   # boolean, 0=False, 1=True


def channel_stats(text):
    """Convert a string list of channel settings into an 
    ordered dictionary: "[['Dev1/ai0', '', '1.0', '0', '0', '0']]"
    -> OrderedDict([('Dev1/ai0', {'label':'', 'offset':1.0,
        'range':0, 'acquire':0, 'plot':0})])
    """
    d = OrderedDict()
    keys = ['label', 'offset', 'range', 'acquire', 'plot']
    types = [str, float, float, BOOL, BOOL]
    for channel in map(strlist, re.findall("\[['\w,\s\./]+\]", text)):
        d[channel[0]] = {keys[i]:types[i](val) for i,val in enumerate(channel[1:])}
    return d

class daq_window(QMainWindow):
    """Window to control and visualise DAQ measurements.
    Set up the desired channels with a given sampling rate.
    Start an acquisition after a trigger. 
    Display the acquired data on a trace, and accumulated
    data on a graph.
    
    Arguments:
    nchan   -- number of input channels to read from
    rate    -- max sample rate in samples / second
    dt      -- desired acquisition period in seconds
    config_file -- path to file storing default settings.
    """
    acquired = pyqtSignal(np.ndarray) # acquired data

    def __init__(self, nchan=1, rate=250e3, dt=0.5, config_file=''):
        super().__init__()
        self.types = OrderedDict([('n_channels',int), ('sample_rate',int), 
            ('acquire_time', int), ('Trigger_channel', str), ('TTL_level', float), 
            ('config_file', str), ('Trigger_edge', str), ('channels',channel_stats)])
        self.stats = OrderedDict([('n_channels', nchan), ('sample_rate', rate/nchan), 
            ('acquire_time', dt), ('Trigger_channel', 'Dev1/ai1'), ('TTL_level', 1.0), # /Dev1/PFI0
            ('config_file', 'monitorconfig.dat'), ('Trigger_edge', 'rising'), 
            ('channels', channel_stats("[['Dev1/ai1', '', '1.0', '5', '1', '1']]'"))])
        self.load_settings(config_file)  # load default settings          
        self.n_samples = int(self.stats['acquire_time'] * self.stats['sample_rate']) # number of samples per acquisition
        self.slave = worker(self.stats['sample_rate'], self.stats['acquire_time'], self.stats['Trigger_channel'], 
                self.stats['TTL_level'], self.stats['Trigger_edge'], list(self.stats['channels'].keys()), 
                [ch['range'] for ch in self.stats['channels'].values()])
        
        self.i = 0  # keeps track of current run number
        self.x = [] # run numbers for graphing collections of acquired data
        self.y = [] # average voltages in slice of acquired trace 

        self.init_UI()
        remove_slot(self.slave.acquired, self.update_trace)

    def load_settings(self, config_file='monitorconfig.dat'):
        """Load the default settings from a config file."""
        pass

    def init_UI(self):
        """Produce the widgets and buttons."""
        self.centre_widget = QWidget()
        self.tabs = QTabWidget()       # make tabs for each main display 
        self.centre_widget.layout = QVBoxLayout()
        self.centre_widget.layout.addWidget(self.tabs)
        self.centre_widget.setLayout(self.centre_widget.layout)
        self.setCentralWidget(self.centre_widget)
        
        # change font size
        font = QFont()
        font.setPixelSize(14)

        #### menubar at top gives options ####
        menubar = self.menuBar()

        #### tab for settings  ####
        settings_tab = QWidget()
        settings_grid = QGridLayout()
        settings_tab.setLayout(settings_grid)
        self.tabs.addTab(settings_tab, "Settings")

        self.settings = QTableWidget(2, 6)
        self.settings.setMinimumSize(1,1)
        self.settings.setHorizontalHeaderLabels(['Duration (ms)', 
            'Sample Rate (kS/s)', 'Trigger Channel', 'Trigger Level (V)', 
            'Trigger Edge', 'Trigger Active?'])
        settings_grid.addWidget(self.settings, 0,0, 1,1)
        defaults = ['500', '250', 'Dev1/ai0', '1.0', 'rising', '1']
        validators = [int_validator, int_validator, None, double_validator, None, bool_validator]
        for i in range(6):
            table_item = QLineEdit(defaults[i])
            table_item.setValidator(validators[i])
            self.settings.setCellWidget(1,i, table_item)
                    
        # on/off
        self.toggle = QPushButton('Start', self)
        self.toggle.setCheckable(True)
        self.toggle.clicked.connect(self.activate)
        settings_grid.addWidget(self.toggle, 1,0, 1,1)

        # channels
        self.channels = QTableWidget(8, 6)
        self.channels.setHorizontalHeaderLabels(['Channel', 'Label', 
            'Offset (V)', 'Range', 'Acquire?', 'Plot?'])
        settings_grid.addWidget(self.channels, 2,0, 1,1)
        validators = [None, double_validator, None, bool_validator, bool_validator]
        for i in range(8):
            chan = 'Dev1/ai'+str(i)
            table_item = QLabel(chan)
            self.channels.setCellWidget(i,0, table_item)
            if chan in self.stats['channels']:
                defaults = self.stats['channels'][chan]
            else:
                defaults = channel_stats("[dummy, , 1.0, 5.0, 0, 0]")['dummy']
            for j, key in zip([0,1,3,4], ['label', 'offset', 'acquire', 'plot']):
                table_item = QLineEdit(str(defaults[key]))
                table_item.setValidator(validators[j])        
                self.channels.setCellWidget(i,j+1, table_item)
            vrange = QComboBox()
            vrange.addItems(['%.1f'%x for x in self.slave.vrs])
            try: vrange.setCurrentIndex(self.slave.vrs.index(defaults['range']))
            except Exception as e: logger.error('Invalid channel voltage range\n'+str(e))
            self.channels.setCellWidget(i,3, vrange)

        #### Plot for most recently acquired trace ####
        trace_tab = QWidget()
        trace_grid = QGridLayout()
        trace_tab.setLayout(trace_grid)
        self.tabs.addTab(trace_tab, "Trace")

        self.trace_canvas = pg.PlotWidget()
        self.trace_legend = self.trace_canvas.addLegend()
        self.trace_canvas.getAxis('bottom').tickFont = font
        self.trace_canvas.getAxis('left').tickFont = font
        self.trace_canvas.setLabel('bottom', 'Time', 's')
        self.trace_canvas.setLabel('left', 'Voltage', 'V')
        trace_grid.addWidget(self.trace_canvas, 0,1, 6,8)

        #### Plot for graph of accumulated data ####
        graph_tab = QWidget()
        graph_grid = QGridLayout()
        graph_tab.setLayout(graph_grid)
        self.tabs.addTab(graph_tab, "Graph")

        self.graph_canvas = pg.PlotWidget()
        self.graph_canvas.getAxis('bottom').tickFont = font
        self.graph_canvas.getAxis('left').tickFont = font
        self.graph_canvas.setLabel('bottom', 'Shot')
        self.graph_canvas.setLabel('left', 'Voltage', 'V')
        graph_grid.addWidget(self.graph_canvas, 0,1, 6,8)

    #### user input functions ####

    def set_duration(self):
        pass

    #### acquisition functions #### 

    def activate(self, toggle=0):
        """Prime the DAQ task for acquisition if it isn't already running.
        Otherwise, stop the task running."""
        if self.toggle.isChecked():
            self.slave.start()
            self.toggle.setText('Stop')
        else:
            self.slave.quit()
            self.toggle.setText('Start')

    #### plotting functions ####

    def update_trace(self, data):
        """Plot the supplied data with labels on the trace canvas."""
        self.trace_canvas.clear()
        # self.trace_legend.clear()
        t = np.linspace(0, self.stats['acquire_time'], self.n_samples)
        i = 0 # index to keep track of which channels have been plotted
        for ch in self.stats['channels'].values():
            if ch['plot']:
                plotitem = self.trace_canvas.plot(t,data[i], name=ch['label'])
                self.trace_legend.addItem(plotitem, ch['label'])
                i += 1

    #### save/load functions ####

    def save_trace(self):
        pass

    def save_graph(self):
        pass
                
####    ####    ####    #### 

def run():
    """Initiate an app to run the program
    if running in Pylab/IPython then there may already be an app instance"""
    app = QApplication.instance()
    standalone = app is None # false if there is already an app instance
    if standalone: # if there isn't an instance, make one
        app = QApplication(sys.argv) 
        
    win = daq_window()
    win.show()
    if standalone: # if an app instance was made, execute it
        sys.exit(app.exec_()) # when the window is closed, the python code also stops
            
if __name__ == "__main__":
    # change directory to this file's location
    run()
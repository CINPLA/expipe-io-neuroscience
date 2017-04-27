"""
Python library for reading OpenEphys files.
Depends on: sys
            os
            glob
            datetime
            numpy
            quantities

Authors: Alessio Buccino @CINPLA,
         Svenn-Arne Dragly @CINPLA,
         Milad H. Mobarhan @CINPLA,
         Mikkel E. Lepperod @CINPLA
"""

# TODO: add extensive funciton descrption and verbose option for prints

from __future__ import division
from __future__ import print_function
from __future__ import with_statement

import quantities as pq
import os
import os.path as op
import numpy as np
from datetime import datetime
import locale
import struct
import platform
from .tools import (_read_python, _cut_to_same_len, _zeros_to_nan, clip_anas,
                    readHeader, loadSpikes, clip_digs, clip_times,
                    clip_tracking, find_nearest, get_number_of_records,
                    read_analog_continuous_signal, read_analog_binary_signals)

# TODO related files
# TODO append .continuous files directly to file and memory map in the end
# TODO ChannelGroup class - needs probe file
# TODO Channel class


# TODO add SYNC and TRACKERSTIM metadata


class Channel:
    def __init__(self, index, name, gain, channel_id):
        self.index = index
        self.id = channel_id
        self.name = name
        self.gain = gain


class AnalogSignal:
    def __init__(self, channel_id, signal, sample_rate):
        self.signal = signal
        self.channel_id = channel_id
        self.sample_rate = sample_rate

    @property
    def times(self):
        if self.signal.shape[0] > 0:
            nsamples = self.signal.shape[1]
            return np.arange(nsamples) / self.sample_rate
        else:
            return np.array([])

    def __str__(self):
        return "<OpenEphys analog signal:shape: {}, sample_rate: {}>".format(
            self.signal.shape, self.sample_rate
        )


class DigitalSignal:
    def __init__(self, times, channel_id, sample_rate):
        self.times = times
        self.channel_id = channel_id
        self.sample_rate = sample_rate

    def __str__(self):
        return "<OpenEphys digital signal: nchannels: {}>".format(
            self.channel_id
        )


class Sync:
    def __init__(self, times, channel_id, sample_rate):
        self.times = times
        self.channel_id = channel_id
        self.sample_rate = sample_rate

    def __str__(self):
        return "<OpenEphys sync signal: nchannels: {}>".format(
            self.channel_id
        )


class TrackingData:
    def __init__(self, times, positions, attrs):
        self.attrs = attrs
        self.times = times
        self.positions = positions

    def __str__(self):
        return "<OpenEphys tracking data: times shape: {}, positions shape: {}>".format(
            self.times.shape, self.positions.shape
        )


class SpikeTrain:
    def __init__(self, times, waveforms,
                 spike_count, channel_count, samples_per_spike,
                 sample_rate, t_stop, **attrs):
        assert(waveforms.shape[0] == spike_count), waveforms.shape[0]
        assert(waveforms.shape[1] == channel_count), waveforms.shape[1]
        assert(waveforms.shape[2] == samples_per_spike), waveforms.shape[2]
        assert(len(times) == spike_count)
        assert times[-1] <= t_stop, ('Spike time {}'.format(times[-1]) +
                                     ' exceeds duration {}'.format(t_stop))
        self.times = times
        self.waveforms = waveforms
        self.attrs = attrs
        self.t_stop = t_stop

        self.spike_count = spike_count
        self.channel_count = channel_count
        self.samples_per_spike = samples_per_spike
        self.sample_rate = sample_rate

    @property
    def num_spikes(self):
        """
        Alias for spike_count.
        """
        return self.spike_count

    @property
    def num_chans(self):
        """
        Alias for channel_count.
        """
        return self.channel_count


class ChannelGroup:
    def __init__(self, channel_group_id, filename, channels,
                 fileclass=None, **attrs):
        self.attrs = attrs
        self.filename = filename
        self.id = channel_group_id
        self.channels = channels
        self.fileclass = fileclass

    def __str__(self):
        return "<OpenEphys channel_group {}: channel_count: {}>".format(
            self.id, len(self.channels)
        )

    @property
    def analog_signals(self):
        ana = self.fileclass.analog_signals[0]
        analog_signals = []
        for channel in self.channels:
            analog_signals.append(AnalogSignal(signal=ana.signal[channel.id],
                                               channel_id=channel.id,
                                               sample_rate=ana.sample_rate))
        return analog_signals

    @property
    def spiketrains(self):
        return [sptr for sptr in self.fileclass.spiketrains
                if sptr.attrs['channel_group_id'] == self.id]


class File:
    """
    Class for reading experimental data from an OpenEphys dataset.
    """
    def __init__(self, foldername, probefile=None):
        import xml.etree.ElementTree as ET
        from xmljson import yahoo as yh
        # TODO assert probefile is a probefile
        # TODO add default prb map and allow to add it later
        self._absolute_foldername = foldername
        self._path, relative_foldername = os.path.split(foldername)
        self._analog_signals_dirty = True
        self._digital_signals_dirty = True
        self._channel_groups_dirty = True
        self._spiketrains_dirty = True
        self._tracking_dirty = True
        self._events_dirty = True
        self._times = []
        self._duration = []

        # TODO: support for multiple exp in same folder
        filenames = [f for f in os.listdir(self._absolute_foldername)]
        if not any(sett == 'settings.xml' for sett in filenames):
            raise ValueError("'setting.xml' should be in the folder")

        if not any('.eventsmessages' in f for f in filenames):
            raise ValueError("'.eventsmessages' should be in the folder")
        else:
            messagefile = [f for f in filenames if '.eventsmessages' in f][0]
            with open(op.join(self._absolute_foldername, messagefile), "r") as fh:
                self._software_sample_rate, self._start_exp = self._read_software_rate(fh)

        self.rhythm = False
        self.rhythmID = []
        rhythmRates = np.array([1., 1.25, 1.5, 2, 2.5, 3, 3.33, 4., 5., 6.25,
                                8., 10., 12.5, 15., 20., 25., 30.])
        self.osc = False
        self.oscInfo = []
        self.tracking_timesamples_rate = 1000 * 1000. * pq.Hz

        self.sync = False
        self.syncID = []

        print('Loading Open-Ephys: reading settings.xml...')
        self._set_fname = op.join(self._absolute_foldername, 'settings.xml')
        with open(self._set_fname) as f:
            xmldata = f.read()
            self.settings = yh.data(ET.fromstring(xmldata))['SETTINGS']
        # read date in US format
        if platform.system() == 'Windows':
            locale.setlocale(locale.LC_ALL, 'english')
        else:
            locale.setlocale(locale.LC_ALL, 'en_US.UTF8')
        self._start_datetime = datetime.strptime(self.settings['INFO']['DATE'], '%d %b %Y %H:%M:%S')
        self._channel_info = {}
        self.nchan = 0
        FPGA_count = 0
        if isinstance(self.settings['SIGNALCHAIN'], list):
            sigchain_iter = self.settings['SIGNALCHAIN']
        else:
            sigchain_iter = [self.settings['SIGNALCHAIN']]
        for sigchain in sigchain_iter:
            if isinstance(sigchain['PROCESSOR'], list):
                processor_iter = sigchain['PROCESSOR']
            else:
                processor_iter = [sigchain['PROCESSOR']]
            for processor in processor_iter:
                # print(processor['name'])
                if processor['name'] == 'Sources/Rhythm FPGA':
                    if FPGA_count > 0:
                        raise NotImplementedError
                        # TODO can there be multiple FPGAs ?
                    FPGA_count += 1
                    self._channel_info['gain'] = {}
                    self.rhythm = True
                    self.rhythmID = processor['NodeId']
                    # gain for all channels
                    gain = {ch['number']: float(ch['gain']) * pq.uV  # TODO assert is uV
                            for chs in processor['CHANNEL_INFO'].values()
                            for ch in chs}
                    for chan in processor['CHANNEL']:
                        if chan['SELECTIONSTATE']['record'] == '1':
                            self.nchan += 1
                            chnum = chan['number']
                            self._channel_info['gain'][chnum] = gain[chnum]
                        sampleIdx = int(processor['EDITOR']['SampleRate'])-1
                        self._sample_rate = rhythmRates[sampleIdx] * 1000. * pq.Hz
                if processor['name'] == 'Sources/OSC Port':
                    self.osc = True
                    self.oscInfo.append({
                        'oscID': processor['NodeId'],
                        'oscPort': processor['EDITOR']['OSCNODE']['port'],
                        'oscAddress': processor['EDITOR']['OSCNODE']['address'],
                    })
                if processor['name'] == 'Sources/Sync Port':
                    self.sync = True
                    self.syncID = processor['NodeId']


        # Check openephys format
        if self.settings['CONTROLPANEL']['recordEngine'] == 'OPENEPHYS':
            self._format = 'openephys'
        elif self.settings['CONTROLPANEL']['recordEngine'] == 'RAWBINARY':
            self._format = 'binary'
        else:
            self._format = None
        print('Decoding data from ', self._format, ' format')

        if self.rhythm:
            print('RhythmFPGA with ', self.nchan, ' channels. NodeId: ', self.rhythmID)
        if self.osc:
            print('OSC Port. NodeId: ', [osc['oscID'] for osc in self.oscInfo])

        if self.rhythm:
            recorded_channels = sorted([int(chan) for chan in
                                        self._channel_info['gain'].keys()])
            self._channel_info['channels'] = recorded_channels
            if probefile is not None:
                self._keep_channels = []
                self._probefile_ch_mapping = _read_python(probefile)['channel_groups']
                for group_idx, group in self._probefile_ch_mapping.items():
                    group['gain'] = []
                    # prb file channels are sequential, 'channels' are not as they depend on FPGA channel selection -> Collapse them into array
                    for chan, oe_chan in zip(group['channels'],
                                             group['oe_channels']):
                        if oe_chan not in recorded_channels:
                            raise ValueError('Channel "' + str(oe_chan) +
                                             '" in channel group "' +
                                             str(group_idx) + '" in probefile' +
                                             probefile +
                                             ' is not marked as recorded ' +
                                             'in settings file' +
                                             self._set_fname)
                        group['gain'].append(
                            self._channel_info['gain'][str(oe_chan)]
                        )
                        self._keep_channels.append(recorded_channels.index(oe_chan))
                print('Number of selected channels: ', len(self._keep_channels))
            else:
                self._keep_channels = None # HACK
                # TODO sequential channel mapping
                print('sequential channel mapping')

    @property
    def session(self):
        return os.path.split(self._absolute_foldername)[-1]

    @property
    def datetime(self):
        return self._start_datetime

    @property
    def duration(self):
        if self.rhythm:
            self._duration = (self.analog_signals[0].signal.shape[1] /
                              self.analog_signals[0].sample_rate)
        elif self.osc:
            self._duration = self.tracking[0].times[0][-1] - self.tracking[0].times[0][0]
        else:
            self._duration = []

        return self._duration

    @property
    def sample_rate(self):
        if self.rhythm:
            return self._sample_rate
        else:
            return self._software_sample_rate

    def channel_group(self, channel_id):
        if self._channel_groups_dirty:
            self._read_channel_groups()

        return self._channel_id_to_channel_group[channel_id]

    @property
    def channel_groups(self):
        if self._channel_groups_dirty:
            self._read_channel_groups()

        return self._channel_groups

    @property
    def analog_signals(self):
        if self._analog_signals_dirty:
            self._read_analog_signals()

        return self._analog_signals

    @property
    def spiketrains(self):
        if self._spiketrains_dirty:
            self._read_spiketrains()

        return self._spiketrains

    @property
    def digital_in_signals(self):
        if self._digital_signals_dirty:
            self._read_digital_signals()

        return self._digital_signals

    @property
    def sync_signals(self):
        if self._digital_signals_dirty:
            self._read_digital_signals()

        return self._sync_signals

    @property
    def events(self):
        if self._events_dirty:
            self._read_digital_signals()

        return self._events

    @property
    def tracking(self):
        if self._tracking_dirty:
            self._read_tracking()

        return self._tracking

    @property
    def times(self):
        if self.rhythmID:
            self._times = self.analog_signals[0].times
        elif self.osc:
            self._times = self.tracking[0].times[0]
        else:
            self._times = []

        return self._times

    def _read_software_rate(self, fh):
        spl = fh.readline().split()
        if any(['Software' in s for s in spl]):
            stime = spl[-1]
            stime = stime.split('@')
            start = stime[0]
            hz_start = stime[-1].find('Hz')
            sample_rate = float(stime[-1][:hz_start]) * pq.Hz
        else:
            start = sample_rate = []

        return sample_rate, start

    def _read_channel_groups(self):
        self._channel_id_to_channel_group = {}
        self._channel_group_id_to_channel_group = {}
        self._channel_count = 0
        self._channel_groups = []
        for channel_group_id, channel_info in self._probefile_ch_mapping.items():
            num_chans = len(channel_info['channels'])
            self._channel_count += num_chans
            channels = []
            for idx, chan in enumerate(channel_info['channels']):
                channel = Channel(
                    index=idx,
                    channel_id=chan,
                    name="channel_{}_channel_group_{}".format(chan,
                                                              channel_group_id),
                    gain=channel_info['gain'][idx]
                )
                channels.append(channel)

            channel_group = ChannelGroup(
                channel_group_id=channel_group_id,
                filename=None,#TODO,
                channels=channels,
                fileclass=self,
                attrs=None #TODO
            )


            self._channel_groups.append(channel_group)
            self._channel_group_id_to_channel_group[channel_group_id] = channel_group

            for chan in channel_info['channels']:
                self._channel_id_to_channel_group[chan] = channel_group

        # TODO channel mapping to file
        self._channel_ids = np.arange(self._channel_count)
        self._channel_groups_dirty = False

    def _read_tracking(self):
        filenames = [f for f in os.listdir(self._absolute_foldername)]
        if self.osc is True and any('.eventsbinary' in f for f in filenames):
            posfile = [f for f in filenames if '.eventsbinary' in f][0]
            print('.eventsbinary: ', posfile)
            with open(op.join(self._absolute_foldername, posfile), "rb") as fh: #, encoding='utf-8', errors='ignore') as fh:
                self._read_tracking_events(fh)
        else:
            raise ValueError("'.eventsbinary' should be in the folder")

    def _read_tracking_events(self, fh):
        print('Reading positions...')

        # TODO consider NOT writing header from openephys
        header = readHeader(fh)

        if float(header['version']) < 0.4:
            raise Exception('Loader is only compatible with .events files with version 0.4 or higher')

        struct_fmt = '=Bq4f'  # uchar, int64, 4floats
        struct_len = struct.calcsize(struct_fmt)
        struct_unpack = struct.Struct(struct_fmt).unpack_from

        nsamples = (os.fstat(fh.fileno()).st_size -fh.tell()) // struct_len
        print('Estimated position samples: ', nsamples)
        nread = 0

        read_data=[]
        while True:
            bytes = fh.read(struct_len)
            if not bytes:
                break
            s = struct_unpack(bytes)
            read_data.append(s)
            nread+=1

        print('Read position samples: ', nread)

        ids, timestamps, x, y, w, h = zip(*read_data)
        ids = np.array(ids)
        timestamps = np.array(timestamps)
        x = np.array(x)
        y = np.array(y)
        w = np.array(w)
        h = np.array(h)

        ts = timestamps / 1000. * pq.s

        # Sort out different Sources
        if len(np.unique(ids)) == 1:
            print("Single tracking source")

            difft = np.diff(ts)
            avg_period = np.mean(difft)
            sample_rate_s = 1. / float(avg_period) * pq.Hz
            x, y, ts = _cut_to_same_len(x, y, ts)
            for i, (xx, yy) in enumerate(zip(x, y)):
                if xx == yy and xx == 0:
                    x[i] = np.nan
                    y[i] = np.nan

            coord_s = [np.array([x, y])]
            ts_s = [ts]

            width_s = np.mean(w)
            height_s = np.mean(h)

            attrs = dict()
            attrs['sample_rate'] = sample_rate_s
            attrs['length_scale'] = np.array([width_s, height_s])
            attrs['oscInfo'] = self.oscInfo.pop()

        else:
            print("Multiple tracking sources")
            sources = np.unique(ids)
            coord_s, w_s, h_s, ts_s = [], [], [], []
            sample_rate_s, width_s, height_s = [], [], []
            for ss in sources:
                x_ = np.squeeze(x[np.where(ids==ss)])
                y_ = np.squeeze(y[np.where(ids==ss)])
                w_ = np.squeeze(w[np.where(ids==ss)])
                h_ = np.squeeze(h[np.where(ids==ss)])
                ts_ = np.squeeze(ts[np.where(ids==ss)])
                difft = np.diff(ts_)
                avg_period = np.mean(difft)
                sample_rate_ = 1. / float(avg_period) * pq.Hz

                # Camera (0,0) is top left corner -> adjust y
                # coord_ = np.array([x_, 1-y_])
                x_, y_, ts_ = _cut_to_same_len(x_, y_, ts_)
                for i, (xx, yy) in enumerate(zip(x_, y_)):
                    if xx == yy and xx == 0:
                        x_[i] = np.nan
                        y_[i] = np.nan

                coord_ = np.array([x_, y_])
                coord_s.append(coord_)
                ts_s.append(ts_)

                sample_rate_s.append(sample_rate_)
                width_s.append(np.mean(w_))
                height_s.append(np.mean(h_))
            attrs = dict()
            attrs['sample_rate'] = np.array(sample_rate_s)
            attrs['length_scale'] = np.transpose(np.array([width_s, height_s]))
            attrs['oscInfo'] = self.oscInfo

        tracking_data = [TrackingData(
            times=ts_s,
            positions=coord_s,
            attrs=attrs
        )]

        self._tracking = tracking_data
        self._tracking_dirty = False

    def _read_analog_signals(self):
        if self.rhythm:
            # Check and decode files
            filenames = [f for f in os.listdir(self._absolute_foldername)]
            if self._format == 'binary':
                if any('.dat' in f for f in filenames):
                    datfile = [f for f in filenames if '.dat' in f and 'experiment' in f][0]
                    print('.dat: ', datfile)
                    with open(op.join(self._absolute_foldername, datfile), "rb") as fh:
                        anas, nsamples = read_analog_binary_signals(fh, self.nchan)
                else:
                    raise ValueError("'experiment_###.dat' should be in the folder")
            elif self._format == 'openephys':
                # Find continuous CH data
                contFiles = [f for f in os.listdir(self._absolute_foldername) if 'continuous' in f and 'CH' in f]
                contFiles = sorted(contFiles)
                if len(contFiles) != 0:
                    print('Reading all channels')
                    anas = np.array([])
                    for f in contFiles:
                        fullpath = op.join(self._absolute_foldername, f)
                        sig = read_analog_continuous_signal(fullpath)
                        if anas.shape[0] < 1:
                            anas = sig['data'][None, :]
                        else:
                            if sig['data'].size == anas[-1].size:
                                anas = np.append(anas, sig['data'][None, :], axis=0)
                            else:
                                raise Exception('Channels must have the same number of samples')
                    assert anas.shape[0] == len(self._channel_info['channels'])
                    nsamples = anas.shape[1]
                    print('Done!')
            # Keep only selected channels
            if self._keep_channels is not None:
                assert anas.shape[1] == nsamples, 'Assumed wrong shape'
                anas_keep = anas[self._keep_channels, :]
            else:
                anas_keep = anas
            self._analog_signals = [AnalogSignal(
                channel_id=range(anas_keep.shape[0]),
                signal=anas_keep,
                sample_rate=self.sample_rate
            )]
        else:
            self._analog_signals = [AnalogSignal(
                channel_id=np.array([]),
                signal=np.array([]),
                sample_rate=self.sample_rate
            )]

        self._analog_signals_dirty = False

    def _read_spiketrains(self):
        if self.rhythm:
            # TODO check if spiketains are recorded from setings
            filenames = [f for f in os.listdir(self._absolute_foldername)
                         if f.endswith('.spikes')]
            self._spiketrains = []
            if len(filenames) == 0:
                return
            for fname in filenames:
                print('Loading spikes from ', fname.split('.')[0])
                data = loadSpikes(op.join(self._absolute_foldername, fname))
                clusters = data['recordingNumber']
                group_id = int(np.unique(data['source']))
                assert 'TT{}'.format(group_id) in fname
                for cluster in np.unique(clusters):
                    wf = data['spikes'][clusters == cluster]
                    wf = wf.swapaxes(1, 2)
                    sample_rate = int(data['header']['sampleRate'])
                    times = data['timestamps'][clusters == cluster] / sample_rate
                    t_stop = self.duration.rescale('s')
                    mask = times <= t_stop
                    if not all(mask):
                        print('Deleted {}'.format(sum(~mask)) +
                              ' spiketimes larger recording duration')
                    self._spiketrains.append(
                        SpikeTrain(
                            times=times[mask] * pq.s,
                            waveforms=wf[mask, :, :] * pq.uV,
                            spike_count=sum(mask),
                            channel_count=int(data['header']['num_channels']),
                            sample_rate=sample_rate * pq.Hz,
                            channel_group_id=group_id,
                            samples_per_spike=40,  # TODO read this from file
                            gain=data['gain'][clusters == cluster],
                            threshold=data['thresh'][clusters == cluster],
                            name='Unit #{}'.format(cluster),
                            cluster_id=int(cluster),
                            t_stop=t_stop
                        )
                    )

        self._spiketrains_dirty = False

    def _read_digital_signals(self):
        filenames = [f for f in os.listdir(self._absolute_foldername)]
        if any('.events' in f and 'all_channels' in f for f in filenames):
            eventsfile = [f for f in filenames if '.events' in f and 'all_channels' in f][0]
            print('.events ', eventsfile)
            with open(op.join(self._absolute_foldername, eventsfile), "rb") as fh: #, encoding='utf-8', errors='ignore') as fh:
                data = {}

                print('loading events...')
                header = readHeader(fh)

                if float(header['version']) < 0.4:
                    raise Exception('Loader is only compatible with .events files with version 0.4 or higher')

                data['header'] = header

                struct_fmt = '=qH4BH'  # int[5], float, byte[255]
                struct_len = struct.calcsize(struct_fmt)
                struct_unpack = struct.Struct(struct_fmt).unpack_from

                nsamples = (os.fstat(fh.fileno()).st_size - fh.tell()) // struct_len
                print('Estimated events samples: ', nsamples)
                nread = 0

                read_data = []
                while True:
                    bytes = fh.read(struct_len)
                    if not bytes:
                        break
                    s = struct_unpack(bytes)
                    read_data.append(s)
                    nread += 1

                print('Read event samples: ', nread)

                timestamps, sampleNum, eventType, nodeId, eventId, channel, recordingNumber = zip(*read_data)

                timestamps = np.array(timestamps)
                sampleNum = np.array(sampleNum)
                nodeId = np.array(nodeId)
                eventType = np.array(eventType)
                eventId = np.array(eventId)
                channel = np.array(channel)
                recordingNumber = np.array(recordingNumber)

                data['channel'] = channel
                data['timestamps'] = timestamps
                data['eventType'] = eventType
                data['nodeId'] = nodeId
                data['eventId'] = eventId
                data['recordingNumber'] = recordingNumber
                data['sampleNum'] = sampleNum

                # TODO: check if data is null (data['event...'] is null?
                # Consider only TTL from FPGA (for now)
                if self.rhythm:
                    if len(data['timestamps']) != 0:
                        idxttl_fpga = np.where((data['eventType'] == 3) & (data['nodeId'] == int(self.rhythmID)))
                        digchan = []
                        digs = []
                        if len(idxttl_fpga[0]) != 0:
                            print('TTLevents: ', len(idxttl_fpga[0]))
                            digchan = np.unique(data['channel'][idxttl_fpga])
                            if len(digchan) == 1:
                                # Single digital input
                                digs = data['timestamps'][idxttl_fpga]
                                # Consider rising edge only
                                digs = digs[::2]
                                # remove start_time (offset) and transform in seconds
                                digs -= data['timestamps'][0]
                                digs = digs.astype(dtype='float')/self.sample_rate
                                digs = np.array([digs]) * pq.s
                            else:
                                for chan in digchan:
                                    idx_chan = np.where(data['channel'] == chan)
                                    new_dig = data['timestamps'][idx_chan]
                                    # Consider rising edge only
                                    new_dig = new_dig[::2]
                                    new_dig -= data['timestamps'][0]
                                    new_dig = new_dig.astype(dtype='float')/self.sample_rate
                                    digs.append(new_dig)
                                digs = np.array(digs * pq.s)

                        self._digital_signals = [DigitalSignal(
                            channel_id=digchan,
                            times=digs,
                            sample_rate=self.sample_rate
                        )]
                    else:
                        self._digital_signals = [DigitalSignal(
                            channel_id=np.array([]),
                            times=np.array([]),
                            sample_rate=[]
                        )]
                else:
                    self._digital_signals = [DigitalSignal(
                        channel_id=np.array([]),
                        times=np.array([]),
                        sample_rate=[]
                    )]

                if self.sync:
                    if len(data['timestamps']) != 0:
                        idxttl_sync = np.where((data['eventType'] == 3) & (data['nodeId'] == int(self.syncID)))
                        syncchan = []
                        syncs = []
                        if len(idxttl_sync[0]) != 0:
                            print('TTL Sync events: ', len(idxttl_sync[0]))
                            syncchan = np.unique(data['channel'][idxttl_sync])
                            if len(syncchan) == 1:
                                # Single digital input
                                syncs = data['timestamps'][idxttl_sync]
                                # remove start_time (offset) and transform in seconds
                                syncs -= data['timestamps'][0]
                                syncs = syncs.astype(dtype='float')/self.sample_rate
                                syncs = np.array([syncs]) * pq.s
                            else:
                                for chan in syncchan:
                                    idx_chan = np.where(data['channel'] == chan)
                                    new_sync = data['timestamps'][idx_chan]

                                    new_sync -= data['timestamps'][0]
                                    new_sync = new_sync.astype(dtype='float')/self.sample_rate
                                    syncs.append(new_sync)
                                syncs = np.array(syncs * pq.s)

                        self._sync_signals = [Sync(
                            channel_id=syncchan,
                            times=syncs,
                            sample_rate=self.sample_rate
                        )]
                    else:
                        self._sync_signals = [DigitalSignal(
                            channel_id=np.array([]),
                            times=np.array([]),
                            sample_rate=[]
                        )]
                else:
                    self._sync_signals = [Sync(
                        channel_id=np.array([]),
                        times=np.array([]),
                        sample_rate=[]
                    )]

                self._digital_signals_dirty = False
                self._events_dirty = False
                # self._events = data


    def clip_recording(self, clipping_times, start_end='start'):

        if clipping_times is not None:
            if clipping_times is not list:
                if type(clipping_times[0]) is not pq.quantity.Quantity:
                    raise AttributeError('clipping_times must be a quantity list of length 1 or 2')

            clipping_times = [t.rescale(pq.s) for t in clipping_times]

            for anas in self.analog_signals:
                anas.signal = clip_anas(anas, self.times, clipping_times, start_end)
            for digs in self.digital_in_signals:
                digs.times = clip_digs(digs, clipping_times, start_end)
                digs.times = digs.times - clipping_times[0]
            for track in self.tracking:
                track.positions, track.times = clip_tracking(track, clipping_times,start_end)

            self._times = clip_times(self._times, clipping_times, start_end)
            self._times -= self._times[0]
            self._duration = self._times[-1] - self._times[0]
        else:
            print('Empty clipping times list.')


    def sync_tracking_from_events(self, ttl_events):
        '''

        :param ttl_events:
        :return:
        '''
        positions = []
        times = []

        for t, (pos, software_ts) in enumerate(zip(self.tracking[0].positions, self.tracking[0].times)):
            # For each software ts find closest ttl_event
            ts = np.zeros(len(software_ts))
            ttl_idx = -1 * np.ones(len(software_ts), dtype='int64')

            for i, s_ts in enumerate(software_ts):
                ts[i], ttl_idx[i] = find_nearest(ttl_events, s_ts)

            # A late osc msg might result in an error -> find second closest timestamp in those cases
            wrong_ts_idx = np.where(np.diff(ts) == 0)[0]
            iteration = 1
            max_iter = 10
            while len(wrong_ts_idx) != 0 and iteration < max_iter:
                print('wrong assignments: ', len(wrong_ts_idx), ' Iteration: ', iteration)
                for i, w_ts in enumerate(wrong_ts_idx):
                    val, idx = find_nearest(ttl_events, software_ts[w_ts], not_in_idx=np.unique(ttl_idx))
                    ts[w_ts] = val[0]
                    ttl_idx[w_ts] = idx[0]
                iteration += 1
                wrong_ts_idx = np.where(np.diff(ts) == 0)[0]

            # substitute missing positions with nans
            missed_ttl = np.ones(len(ttl_events), dtype=bool)
            missed_ttl[ttl_idx] = False
            new_pos = np.zeros((pos.shape[0], len(ttl_events)))
            new_pos[:, ttl_idx]  = pos
            new_pos[:, missed_ttl] = np.nan

            positions.append(new_pos)
            times.append(ttl_events)

        self.tracking[0].positions = positions
        self.tracking[0].times = times
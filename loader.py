from minerl.data import DataPipeline
import torch.multiprocessing as mp
from itertools import cycle
from minerl.data.util import minibatch_gen
import minerl
import torch
from random import shuffle
import os

def loader(files, pipe, main_sem, internal_sem, consumed_sem, batch_size):
    
    files = cycle(files)
    while True:
        f = next(files)
        
        try:
            d = DataPipeline._load_data_pyfunc(f, -1, None)
        except:
            continue
        pipe.send("RESET")
        steps = 0
        obs, act, reward, nextobs, done = d
        #print(len(obs["pov"]))
        #print("start")
        obs_screen = torch.tensor(obs["pov"], dtype=torch.float32).unsqueeze(1).transpose(2,4)
        #print("pov")
        obs_vector = torch.tensor(obs["vector"], dtype=torch.float32).unsqueeze(1)#.transpose(0,1)
        #print("vec")
        actions = torch.tensor(act["vector"], dtype=torch.float32).unsqueeze(1)#.transpose(0,1)
        l = actions.shape[0]
        for i in range(0, l, batch_size):
            steps += 1
            #print(steps)
            
            #output = seg_batch['obs'], seg_batch['act'], seg_batch['reward'], seg_batch['next_obs'], seg_batch['done']
            if l < i+batch_size:
                #print("wut", len(obs["pov"][i:i+batch_size]))
                break
            
            pipe.send((obs_screen[i:i+batch_size], obs_vector[i:i+batch_size], actions[i:i+batch_size]))
            
            internal_sem.release()
            main_sem.release()
            consumed_sem.acquire()



class ReplayRoller():

    def __init__(self, files_queue, model, sem, batch_size, prefetch):
        self.batch_size = batch_size
        self.sem = sem
        self.model = model
        self.in_sem = mp.Semaphore(0)
        self.sem_consumed = mp.Semaphore(prefetch)
        self.data = []
        self.hidden = self.model.get_zero_state(1)
        #print(self.hidden)
        self.hidden = (self.hidden[0].cuda(),self.hidden[1].cuda())
        self.pipe_my, pipe_other = mp.Pipe()
        self.files = files_queue
        self.loader = mp.Process(target=loader,args=(self.files,pipe_other,self.sem,self.in_sem, self.sem_consumed, self.batch_size))
        self.loader.start()


    def get(self):
        
        if not self.in_sem.acquire(block=False):
            return []

        data = self.pipe_my.recv()

        while data == "RESET":
            self.hidden = self.model.get_zero_state(1)
            self.hidden = (self.hidden[0].cuda(),self.hidden[1].cuda())
            data = self.pipe_my.recv()

        return data + (self.hidden,)


    def set_hidden(self, new_hidden):
        self.sem_consumed.release()
        self.hidden = new_hidden


class BatchSeqLoader():
    '''
    Brilliant solution that maximizes sample diversity and guarantees that your network won't be able to use its memory
    '''

    def __init__(self, envs, names, steps, model):
        self.main_sem = mp.Semaphore(0)
        self.rollers = []

        def chunkIt(seq, num):
            avg = len(seq) / float(num)
            out = []
            last = 0.0

            while last < len(seq):
                out.append(seq[int(last):int(last + avg)])
                last += avg

            return out

        names = chunkIt(names, envs)

        for i in range(envs):
            self.rollers.append(ReplayRoller(names[i], model, self.main_sem, steps, 1))    
    
    def batch_lstm(self,states):
        states = zip(*states)
        return tuple([torch.cat(s,1) for s in states])

    def unbatch_lstm(self,state):
        l = state[0].shape[1]
        output = []
        for i in range(l):
            output.append((state[0][:,i:i+1].detach(), state[1][:,i:i+1].detach()))

        return output

    def get_batch(self, batch_size):

        shuffle(self.rollers)
        data, self.current_rollers = [],[]
        while len(data) < batch_size:
            self.main_sem.acquire()
            for roller in self.rollers:
                maybe_data = roller.get()
                if len(maybe_data) > 0:
                    sample = maybe_data
                    data.append(sample)
                    self.current_rollers.append(roller)
                    if len(data) == batch_size:
                        break

        obs_screen, obs_vector, act, states = zip(*data)

        obs_screen = torch.cat(obs_screen, dim=1).cuda()
        obs_vector = torch.cat(obs_vector, dim=1).cuda()
        act = torch.cat(act, dim=1).cuda()
        #print(act.shape)
        return obs_screen, obs_vector, act, self.batch_lstm(states)

    def put_back(self, lstm_state):
        lstm_state = self.unbatch_lstm(lstm_state)
        for i, roller in enumerate(self.current_rollers):
            roller.set_hidden(lstm_state[i])

class dummy_model:

    def get_zero_state(self, x):
        return (torch.zeros((1,1,1)),torch.zeros((1,1,1)))


def absolute_file_paths(directory):
    return [os.path.join(directory, path) for path in os.listdir(directory)]


if __name__ == "__main__":
    data = minerl.data.make('MineRLObtainDiamondVectorObf-v0', data_dir='data/',num_workers=6)
    model = dummy_model()
    loader = BatchSeqLoader(1, data._get_all_valid_recordings('data/MineRLObtainDiamondVectorObf-v0'), 128, model)
    i = 0
    while True:
        i+=1
        print(i)
        _,_,_,data = loader.get_batch(1)
        loader.put_back(data)
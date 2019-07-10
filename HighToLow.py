import shutil
import random
import os

import torch
from torch.backends import cudnn
from torch.autograd import Variable
import torchvision.utils as vutils

import config
from base import BaseAgent
from Generator import HighToLowGenerator
from Discriminator import HighToLowDiscriminator
from loss import MSELoss
from dataset import get_loader

from tensorboardX import SummaryWriter
from misc import print_cuda_statistics

cudnn.benchmark = True

class HighToLow(BaseAgent):
    def __init__(self, config):
        super().__init__(config)

        # define models (generator and discriminator)
        self.netG = HighToLowGenerator()
        self.netD = HighToLowDiscriminator()

        # define loss
        #self.loss = GANLoss()
        #self.loss = HingeEmbeddingLoss()
        self.criterion_GAN = torch.nn.BCEWithLogitsLoss()
        self.criterion_MSE = MSELoss()
        
        # define optimizers for both generator and discriminator
        self.optimG = torch.optim.Adam(self.netG.parameters(), lr=self.config.learning_rate, betas=(self.config.beta1, self.config.beta2))
        self.optimD = torch.optim.Adam(self.netD.parameters(), lr=self.config.learning_rate, betas=(self.config.beta1, self.config.beta2))
        
        # initialize counter
        self.current_epoch = 0
        self.current_iteration = 0
        self.best_valid_mean_iou = 0
        
        self.real_label = 1
        self.fake_label = -1
        
        # set cuda flag
        self.is_cuda = torch.cuda.is_available()
        if self.is_cuda and not self.config.cuda:
            self.logger.info("WARNING: You have a CUDA device, so you should probably enable CUDA")

        self.cuda = self.is_cuda & self.config.cuda
        
        # set the manual seed for torch
        self.manual_seed = random.randint(1, 10000)
        self.logger.info ('seed:{}'.format(self.manual_seed))
        random.seed(self.manual_seed)
        
        self.test_file = self.config.output_path
        if not os.path.exists(self.test_file):
            os.makedirs(self.test_file)

        if self.cuda:
            self.device = torch.device("cuda")
            torch.cuda.set_device(self.config.gpu_device)
            torch.cuda.manual_seed_all(self.manual_seed)
            self.logger.info("Program will run on *****GPU-CUDA***** ")
            print_cuda_statistics()
        else:
            self.device = torch.device("cpu")
            torch.manual_seed(self.manual_seed)
            self.logger.info("Program will run on *****CPU***** ")

        self.netG = self.netG.to(self.device)
        self.netD = self.netD.to(self.device)
        self.criterion_GAN = self.criterion_GAN.to(self.device)
        self.criterion_MSE = self.criterion_MSE.to(self.device)

        # Model Loading from the latest checkpoint if not found start from scratch.
        self.load_checkpoint(self.config.checkpoint_file)

        # Summary Writer
        self.summary_writer = SummaryWriter(log_dir=self.config.summary_dir, comment='High-To-Low GAN')
        
    def load_checkpoint(self, file_name):
        filename = self.config.checkpoint_dir + file_name
        try:
            self.logger.info("Loading checkpoint '{}'".format(filename))
            checkpoint = torch.load(filename)

            self.current_epoch = checkpoint['epoch']
            self.current_iteration = checkpoint['iteration']
            self.netG.load_state_dict(checkpoint['G_state_dict'])
            self.optimG.load_state_dict(checkpoint['G_optimizer'])
            self.netD.load_state_dict(checkpoint['D_state_dict'])
            self.optimD.load_state_dict(checkpoint['D_optimizer'])
            self.manual_seed = checkpoint['manual_seed']

            self.logger.info("Checkpoint loaded successfully from '{}' at (epoch {}) at (iteration {})\n"
                  .format(self.config.checkpoint_dir, checkpoint['epoch'], checkpoint['iteration']))
        except OSError:
            self.logger.info("No checkpoint exists from '{}'. Skipping...".format(self.config.checkpoint_dir))
            self.logger.info("**First time to train**")

    def save_checkpoint(self, file_name="checkpoint.pth.tar", is_best = 0):
        state = {
            'epoch': self.current_epoch,
            'iteration': self.current_iteration,
            'G_state_dict': self.netG.state_dict(),
            'G_optimizer': self.optimG.state_dict(),
            'D_state_dict': self.netD.state_dict(),
            'D_optimizer': self.optimD.state_dict(),
            'manual_seed': self.manual_seed
        }

        # Save the state
        torch.save(state, self.config.checkpoint_dir + file_name)
        # If it is the best copy it to another file 'model_best.pth.tar'
        if is_best:
            shutil.copyfile(self.config.checkpoint_dir + file_name,
                            self.config.checkpoint_dir + 'High_to_Low_model_best.pth.tar')
            
    def run(self):
        """
        This function will the operator
        :return:
        """
        try:
            self.train()
        except KeyboardInterrupt:
            self.logger.info("You have entered CTRL+C.. Wait to finalize")

    def train(self):
        for epoch in range(self.current_epoch, self.config.max_epoch):
            self.current_epoch = epoch
            self.train_one_epoch()
            self.save_checkpoint()
            
    def to_var(self, data):
        real_cpu = data
        batchsize = real_cpu.size(0)
        inp = Variable(real_cpu.cuda())
        return inp, batchsize
    
    def train_one_epoch(self):
        test_loader = get_loader(self.config.HighToLow_hr_datapath, 
                                 self.config.HighToLow_lr_datapath, 
                                 self.config.batch_size)
        
        self.netG.train()
        self.netD.train()

        for curr_it, data_dict in enumerate(test_loader):
            #y = torch.full((self.batch_size,), self.real_label)
            data_low = data_dict['lr']
            data_high = data_dict['hr']
            data_high_low = data_dict['hlr']
            data_input_low, batchsize = self.to_var(data_low)
            data_input_high, _ = self.to_var(data_high)
            data_input_high_low, _ = self.to_var(data_high_low)
            
            y = torch.randn(data_low.size(0), )
            y, _ = self.to_var(y)
            
            ##################
            #  Train Generator
            ##################

            self.optimG.zero_grad()
    
            # Generate a high resolution image from low resolution input
            noise = torch.randn(data_high.size(0), 1)
            noise, _ = self.to_var(noise)
            gen_lr = self.netG(data_input_high, noise)
    
            # Measure pixel-wise loss against ground truth
            loss_pixel = self.criterion_MSE(gen_lr, data_input_high_low)
            
            # Extract validity predictions from discriminator
            pred_real = self.netD(data_input_high).detach()
            pred_fake = self.netD(gen_lr)

            # Adversarial loss (relativistic average GAN)
            y.fill_(self.real_label)
            loss_G_GAN = self.criterion_GAN(pred_fake - pred_real.mean(0, keepdim=True), y)
            
            # Total generator loss
            loss_G = (self.config.beta * loss_G_GAN) + (self.config.alpha * loss_pixel)
            
            loss_G.backward(retain_graph=True)
            self.optimG.step()

            ######################
            #  Train Discriminator
            ######################
            
            self.optimD.zero_grad()

            # Adversarial loss for real and fake images (relativistic average GAN)
            pred_real = self.netD(data_input_high)
            y.fill_(self.real_label)
            loss_D_real = self.criterion_GAN(pred_real - pred_fake.mean(0, keepdim=True), y)
            loss_D_real.backward(retain_graph=True)
            
            pred_fake = self.netD(gen_lr.detach())
            y.fill_(self.fake_label)
            loss_D_fake = self.criterion_GAN(pred_fake - pred_real.mean(0, keepdim=True), y)
            loss_D_fake.backward()
            # Total loss
            loss_D = (loss_D_real + loss_D_fake) / 2
    
            self.optimD.step()
            
            self.current_iteration += 1

            self.summary_writer.add_scalar("epoch/Generator_loss", loss_G.item(), self.current_iteration)
            self.summary_writer.add_scalar("epoch/Discriminator_loss_real", loss_D_real.item(), self.current_iteration)
            self.summary_writer.add_scalar("epoch/Discriminator_loss_fake", loss_D_fake.item(), self.current_iteration)
            
            path = os.path.join(self.test_file, 'batch' + str(curr_it) + '_epoch'+ str(self.current_epoch) + '.jpg')
            vutils.save_image(gen_lr.data, path, normalize=True)
            
            # --------------
            #  Log Progress
            # --------------
    
            self.logger.info(
                "[Epoch %d/%d] [Batch %d/%d] [D loss: %f, real: %f, fake: %f] [G loss: %f, adv: %f, pixel: %f]"
                % (
                    self.current_epoch + 1,
                    self.config.max_epoch,
                    curr_it + 1,
                    len(test_loader),
                    loss_D.item(),
                    loss_D_real.item(),
                    loss_D_fake.item(),
                    loss_G.item(),
                    loss_G_GAN.item(),
                    loss_pixel.item(),
                )
            )

    def validate(self):
        pass

    def finalize(self):
        """
        Finalize all the operations of the 2 Main classes of the process the operator and the data loader
        :return:
        """
        self.logger.info("Please wait while finalizing the operation.. Thank you")
        self.save_checkpoint()
        self.summary_writer.export_scalars_to_json("{}all_scalars.json".format(self.config.summary_dir))
        self.summary_writer.close()
        self.dataloader.finalize()
        
if __name__ == "__main__":
    config_dir = config.process_config('configurations/train_config.json')
    gan = HighToLow(config_dir)
    gan.run()
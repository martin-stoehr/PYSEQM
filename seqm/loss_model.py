import torch
from warnings import warn
from torch.autograd import grad as agrad
from torch.nn.utils.rnn import pad_sequence
from .basics import Parser, Energy
from .seqm_functions.constants import Constants

LFAIL = torch.tensor(torch.inf)
LIST_OF_TENSORS_MSG = "Input has to be provided as list of torch.Tensor's!"

torch.set_default_dtype(torch.float64)
has_cuda = torch.cuda.is_available()
if has_cuda:
    device = torch.device('cuda')
    sp2_def = [True, 1e-5]
else:
    device = torch.device('cpu')
    sp2_def = [False]

prop2index = {'gap':3, 'forces':2, 'atomization':0, 'energy':1}


class LossConstructor(torch.nn.Module):
    def __init__(self, popt_list=None, species=None, coordinates=None,
                 custom_settings=None):
        ## initialize parent module and attributes
        super(LossConstructor, self).__init__()
        self.implemented = ['energy', 'forces', 'gap', 'atomization']
        self.n_implemented = len(self.implemented)
        self.include = [False,]*self.n_implemented
        self.weights = torch.zeros(self.n_implemented)
        
        ## collect attributes from input
        self.check_input(species)
        self.species = pad_sequence(species, batch_first=True)
        self.nAtoms = torch.count_nonzero(self.species, dim=1)
        elements = [0]+sorted(set(self.species.reshape(-1).tolist()))
        self.check_input(coordinates)
        self.coordinates = pad_sequence(coordinates, batch_first=True)
        self.coordinates.requires_grad_(True)
        settings = {
                    'method'             : 'AM1',
                    'scf_eps'            : 1.0e-6,
                    'scf_converger'      : [0,0.15],
                    'sp2'                : sp2_def,
                    'pair_outer_cutoff'  : 1.0e10,
                    'Hf_flag'            : False,
                   }
        settings.update(custom_settings)
        settings['elements'] = torch.tensor(elements)
        settings['learned'] = popt_list
        settings['eig'] = True
        self.custom_params = popt_list
        self.const = Constants()#.to(device)
        self.calc = Energy(settings)#.to(device)
    
    def __eq__(self, other):
        if self.__class__ != other.__class__: return False
        return self.__dict__ == other.__dict__
    
    def __call__(self, p):
        return self.forward(p)
    
#    @staticmethod
    def forward(self, p):
#    def forward(self, ctx, p):
        """ Get Loss. """
        if not any(self.include): raise RuntimeError("Need to add a loss property!")
        learnedpar = {pname:p[i] for i, pname in enumerate(self.custom_params)}
        Deltas = torch.zeros(self.n_implemented)
        try:
            res = self.calc(self.const, self.coordinates, self.species, 
                            learnedpar, all_terms=True)
        except RuntimeError:
#            ctx.save_for_backward(True)
            return LFAIL
        
        masking = (~res[-1]).float()        
        if self.include[0]:
            DeltaA2 = torch.square(res[0] - self.atomization_ref) / self.nAtoms
            Deltas[0] = (DeltaA2 * masking).sum()
        if self.include[1]:
            DeltaE2 = torch.square(res[1] - self.energy_ref) / self.nAtoms
            Deltas[1] = (DeltaE2 * masking).sum()
        if self.include[2]:
            F = -agrad(res[1].sum(), self.coordinates, create_graph=True)[0]
            DeltaF2 = torch.square(F - self.forces_ref).sum(dim=(1,2)) / self.nAtoms
            Deltas[2] = (DeltaF2 * masking).sum()
        if self.include[3]:
            parser = Parser(self.calc.seqm_parameters)
            n_occ = parser(self.const, self.species, self.coordinates)[:,4]
            homo, lumo = n_occ - 1, n_occ
            orb_eigs = res[6]
            gap = orb_eigs[:,lumo] - orb_eigs[:,homo]
            DeltaG2 = torch.square(gap - self.gap_ref)
            Deltas[3] = (DeltaG2 * masking).sum()
#        ctx.save_for_backward(False)
        return (Deltas * self.weights).sum()
    
#    @staticmethod
#    def backward(ctx, grad_in)
#        if grad_in is None: return torch.zeros_like(grad_in)
#        SCFfailed = ctx.saved_tensors
#        if SCFfailed: return torch.ones_like(grad_in)*LFAIL
#        return ???
    
    def add_loss(self, prop, prop_ref, weight=1.):
        """
        Add individual loss evaluators as defined above to loss function.
        If implementing a new property, please add loss functon
        `<property>_loss(...)` above and update self.implemented_properties
        """
        if prop not in self.implemented:
            msg  = "Only '"+"', '".join(self.implemented_properties)
            msg += "' implemented for loss. Check for typos or write "
            msg += "coresponding loss function for '"+prop+"'."
            raise ValueError(msg)
        if prop == 'gap':
            msg  = 'HOMO-LUMO gap explicitly depends on eigenvalues. '
            msg += 'These might have derivative discontinuities w.r.t. '
            msg += 'SEQM parameters (MOs crossing) -> unlikely, but '
            msg += 'possible instabilities in autograd!'
            warn(msg)
        
        self.weights[prop2index[prop]] = weight
        if prop == 'force': # pad forces to turn into tensor
            exec('self.'+prop+'_ref = pad_sequence(prop_ref, batch_first=True)')
        else:
            exec('self.'+prop+'_ref = torch.tensor(prop_ref)')
        self.include[prop2index[prop]] = True
    
    def check_input(self, inp):
        """ Check that input is list of tensors (required for full autograd). """
        check1 = type(inp) == list
        check2 = all(torch.is_tensor(i) for i in inp)
        if not (check1 and check2):
            raise ValueError(LIST_OF_TENSORS_MSG)



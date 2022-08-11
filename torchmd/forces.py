from scipy import constants as const
import torch
import numpy as np
from math import pi
import os
import tables as t


class Forces:
    """
    Parameters
    ----------
    cutoff : float
        If set to a value it will only calculate LJ, electrostatics and bond energies for atoms which are closer
        than the threshold
    rfa : bool
        Use with `cutoff` to enable the reaction field approximation for scaling of the electrostatics up to the cutoff.
        Uses the value of `solventDielectric` to model everything beyond the cutoff distance as solvent with uniform
        dielectric.
    solventDielectric : float
        Used together with `cutoff` and `rfa`
    """

    # 1-4 is nonbonded but we put it currently in bonded to not calculate all distances
    bonded = ["bonds", "angles", "dihedrals", "impropers", "1-4"]
    nonbonded = ["electrostatics", "lj", "repulsion", "repulsioncg"]
    terms = bonded + nonbonded

    def __init__(
        self,
        parameters,
        terms=None,
        external=None,
        cutoff=None,
        rfa=False,
        solventDielectric=78.5,
        switch_dist=None,
        exclusions=("bonds", "angles", "1-4"),
    ):
        self.par = parameters
        if terms is None:
            raise RuntimeError(
                'Set force terms or leave empty brackets [].\nAvailable options: "bonds", "angles", "dihedrals", "impropers", "1-4", "electrostatics", "lj", "repulsion", "repulsioncg".'
            )

        self.energies = [ene.lower() for ene in terms]
        for et in self.energies:
            if et not in Forces.terms:
                raise ValueError(f"Force term {et} is not implemented.")

        if "1-4" in self.energies and "dihedrals" not in self.energies:
            raise RuntimeError(
                "You cannot enable 1-4 interactions without enabling dihedrals"
            )

        self.natoms = len(parameters.masses)
        self.require_distances = any(f in self.nonbonded for f in self.energies)
        self.ava_idx = (
            self._make_indeces(
                self.natoms, parameters.get_exclusions(exclusions), parameters.device
            )
            if self.require_distances
            else None
        )
        self.neighborlist = None
        self.external = external
        self.cutoff = cutoff
        self.rfa = rfa
        self.solventDielectric = solventDielectric
        self.switch_dist = switch_dist

    def _filter_by_cutoff(self, dist, arrays):
        under_cutoff = dist <= self.cutoff
        indexedarrays = []
        for arr in arrays:
            indexedarrays.append(arr[under_cutoff])
        return indexedarrays
    
    def _neighbor_verlet_list(self, dist, arrays, delt_r):
        neighbor = dist <= self.cutoff + delt_r
        indexedarrays = []
        for arr in arrays:
            indexedarrays.append(arr[neighbor])
        return indexedarrays

    def compute(self, pos, box, forces, returnDetails=False, explicit_forces=True, itstep = None, reconstep = None, delt_r = None):
        #I plus three more values
        ## itstep: iteration step, the times of iteration, must start from 0.
        ## reconstep: reconstruction step, after these steps, the verlet list need to reconstruct
        ## delt_r: the delta radius out of the cutoff in order to consider enough molecules until reconstruction step.
        if not explicit_forces and not pos.requires_grad:
            raise RuntimeError(
                "The positions passed don't require gradients. Please use pos.detach().requires_grad_(True) before passing."
            )

        nsystems = pos.shape[0]
        if torch.any(torch.isnan(pos)):
            raise RuntimeError("Found NaN coordinates.")

        pot = []
        for i in range(nsystems):
            pp = {
                v: torch.zeros(1, device=pos.device).type(pos.dtype)
                for v in self.energies
            }
            pp["external"] = torch.zeros(1, device=pos.device).type(pos.dtype)
            pot.append(pp)

        forces.zero_()
        for i in range(nsystems):
            spos = pos[i]
            sbox = box[i][torch.eye(3).bool()]  # Use only the diagonal

            # Bonded terms
            # TODO: We are for sure doing duplicate distance calculations here!
            if "bonds" in self.energies and self.par.bonds is not None:
                bond_dist, bond_unitvec, _ = calculate_distances(
                    spos, self.par.bonds, sbox
                )
                pairs = self.par.bonds
                bond_params = self.par.bond_params
                if self.cutoff is not None:
                    (
                        bond_dist,
                        bond_unitvec,
                        pairs,
                        bond_params,
                    ) = self._filter_by_cutoff(
                        bond_dist, (bond_dist, bond_unitvec, pairs, bond_params)
                    )
                E, force_coeff = evaluate_bonds(bond_dist, bond_params, explicit_forces)

                pot[i]["bonds"] += E.sum()
                if explicit_forces:
                    forcevec = bond_unitvec * force_coeff[:, None]
                    forces[i].index_add_(0, pairs[:, 0], -forcevec)
                    forces[i].index_add_(0, pairs[:, 1], forcevec)

            if "angles" in self.energies and self.par.angles is not None:
                _, _, r21 = calculate_distances(spos, self.par.angles[:, [0, 1]], sbox)
                _, _, r23 = calculate_distances(spos, self.par.angles[:, [2, 1]], sbox)
                E, angle_forces = evaluate_angles(
                    r21, r23, self.par.angle_params, explicit_forces
                )

                pot[i]["angles"] += E.sum()
                if explicit_forces:
                    forces[i].index_add_(0, self.par.angles[:, 0], angle_forces[0])
                    forces[i].index_add_(0, self.par.angles[:, 1], angle_forces[1])
                    forces[i].index_add_(0, self.par.angles[:, 2], angle_forces[2])

            if "dihedrals" in self.energies and self.par.dihedrals is not None:
                _, _, r12 = calculate_distances(
                    spos, self.par.dihedrals[:, [0, 1]], sbox
                )
                _, _, r23 = calculate_distances(
                    spos, self.par.dihedrals[:, [1, 2]], sbox
                )
                _, _, r34 = calculate_distances(
                    spos, self.par.dihedrals[:, [2, 3]], sbox
                )
                E, dihedral_forces = evaluate_torsion(
                    r12, r23, r34, self.par.dihedral_params, explicit_forces
                )

                pot[i]["dihedrals"] += E.sum()
                if explicit_forces:
                    forces[i].index_add_(
                        0, self.par.dihedrals[:, 0], dihedral_forces[0]
                    )
                    forces[i].index_add_(
                        0, self.par.dihedrals[:, 1], dihedral_forces[1]
                    )
                    forces[i].index_add_(
                        0, self.par.dihedrals[:, 2], dihedral_forces[2]
                    )
                    forces[i].index_add_(
                        0, self.par.dihedrals[:, 3], dihedral_forces[3]
                    )

            if "1-4" in self.energies and self.par.idx14 is not None:
                nb_dist, nb_unitvec, _ = calculate_distances(spos, self.par.idx14, sbox)

                nonbonded_14_params = self.par.nonbonded_14_params
                idx14 = self.par.idx14
                # if self.cutoff is not None:
                #     (
                #         nb_dist,
                #         nb_unitvec,
                #         nonbonded_14_params,
                #         idx14,
                #     ) = self._filter_by_cutoff(
                #         nb_dist,
                #         (
                #             nb_dist,
                #             nb_unitvec,
                #             self.par.nonbonded_14_params,
                #             self.par.idx14,
                #         ),
                #     )

                aa = nonbonded_14_params[:, 0]
                bb = nonbonded_14_params[:, 1]
                scnb = nonbonded_14_params[:, 2]
                scee = nonbonded_14_params[:, 3]

                if "lj" in self.energies:
                    E, force_coeff = evaluate_LJ_internal(
                        nb_dist, aa, bb, scnb, None, None, explicit_forces
                    )
                    pot[i]["lj"] += E.sum()
                    if explicit_forces:
                        forcevec = nb_unitvec * force_coeff[:, None]
                        forces[i].index_add_(0, idx14[:, 0], -forcevec)
                        forces[i].index_add_(0, idx14[:, 1], forcevec)
                if "electrostatics" in self.energies:
                    E, force_coeff = evaluate_electrostatics(
                        nb_dist,
                        idx14,
                        self.par.charges,
                        scee,
                        cutoff=None,
                        rfa=False,
                        solventDielectric=self.solventDielectric,
                        explicit_forces=explicit_forces,
                    )
                    pot[i]["electrostatics"] += E.sum()
                    if explicit_forces:
                        forcevec = nb_unitvec * force_coeff[:, None]
                        forces[i].index_add_(0, idx14[:, 0], -forcevec)
                        forces[i].index_add_(0, idx14[:, 1], forcevec)
                del aa, bb, scnb, scee, force_coeff

            if "impropers" in self.energies and self.par.impropers is not None:
                _, _, r12 = calculate_distances(
                    spos, self.par.impropers[:, [0, 1]], sbox
                )
                _, _, r23 = calculate_distances(
                    spos, self.par.impropers[:, [1, 2]], sbox
                )
                _, _, r34 = calculate_distances(
                    spos, self.par.impropers[:, [2, 3]], sbox
                )
                E, improper_forces = evaluate_torsion(
                    r12, r23, r34, self.par.improper_params, explicit_forces
                )

                pot[i]["impropers"] += E.sum()
                if explicit_forces:
                    forces[i].index_add_(
                        0, self.par.impropers[:, 0], improper_forces[0]
                    )
                    forces[i].index_add_(
                        0, self.par.impropers[:, 1], improper_forces[1]
                    )
                    forces[i].index_add_(
                        0, self.par.impropers[:, 2], improper_forces[2]
                    )
                    forces[i].index_add_(
                        0, self.par.impropers[:, 3], improper_forces[3]
                    )
                del r12, r23, r34, E, improper_forces
                torch.cuda.empty_cache()

            # Non-bonded terms
            if self.ava_idx == None:
                ffile = t.open_file('non-interactions.h5', 'r')
                idx = ffile.root.data
                if self.require_distances and len(idx):
                    import pynvml
                    pynvml.nvmlInit()
                    p = 0
                    p1 = 0
                    a1 = torch.cuda.memory_allocated()
                    asingle = torch.tensor([1,1]).to(self.par.device)
                    a2 = torch.cuda.memory_allocated()
                    d = asingle.get_device()
                    handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                    meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    p = int(meminfo.free/(a2-a1))
                        
                    if itstep != None and self.cutoff != None:
                        if reconstep == None:
                            reconstep = 10 #reconstep is 10 by default
                        if reconstep <= 1:
                            raise ValueError(" reconstep can not less than 2")
                        if itstep % reconstep == 0:
                            while p < len(idx):
                                self.neighborlist = torch.tensor([[]]*2, dtype=int).T.to(self.par.device)
                                ava_idx = torch.tensor(idx[p1:p].astype(int)).to(self.par.device)
                                nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                if delt_r == None:
                                    delt_r = self.cutoff
                                _, _, vl = self._neighbor_verlet_list(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                )
                                self.neighborlist = torch.cat((self.neighborlist,vl), axis = 0)
                                torch.cuda.empty_cache()
                                handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                                meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                                p1 = p
                                p = p + int(meminfo.free/(a2-a1))

                            if p >= len(idx):
                                ava_idx = torch.tensor(idx[p1:].astype(int)).to(self.par.device)
                                nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                if delt_r == None:
                                    delt_r = self.cutoff
                                _, _, vl = self._neighbor_verlet_list(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                )
                                self.neighborlist = torch.cat((self.neighborlist,vl), axis = 0)
                        
                        if self.neighborlist == None:
                            raise ValueError("itration step should start from 0")
                        nbv_dist, nbv_unitvec, _ = calculate_distances(spos, self.neighborlist, sbox)
                        nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                            nbv_dist, (nbv_dist, nbv_unitvec, self.neighborlist)
                        )
                        for v in self.energies:
                            if v == "electrostatics":
                                E, force_coeff = evaluate_electrostatics(
                                    nb_dist,
                                    ava_idx,
                                    self.par.charges,
                                    cutoff=self.cutoff,
                                    rfa=self.rfa,
                                    solventDielectric=self.solventDielectric,
                                    explicit_forces=explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            elif v == "lj":
                                E, force_coeff = evaluate_LJ(
                                    nb_dist,
                                    ava_idx,
                                    self.par.mapped_atom_types,
                                    self.par.A,
                                    self.par.B,
                                    self.switch_dist,
                                    self.cutoff,
                                    explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            elif v == "repulsion":
                                E, force_coeff = evaluate_repulsion(
                                    nb_dist,
                                    ava_idx,
                                    self.par.mapped_atom_types,
                                    self.par.A,
                                    explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            elif v == "repulsioncg":
                                E, force_coeff = evaluate_repulsion_CG(
                                    nb_dist,
                                    ava_idx,
                                    self.par.mapped_atom_types,
                                    self.par.B,
                                    explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            else:
                                continue
                            
                            if explicit_forces:
                                forcevec = nb_unitvec * force_coeff[:, None]
                                forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                        del nb_dist, nb_unitvec, ava_idx
                        torch.cuda.empty_cache()
                        pynvml.nvmlShutdown()

                    else:
                        while p < len(idx):
                            ava_idx = torch.tensor(idx[p1:p].astype(int)).to(self.par.device)
    #breakpoint                        print(p)
                            nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
    #breakpoint                        print('*')
                            if self.cutoff is not None:
                                nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                )
    #breakpoint                        print(ava_idx)
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                            del nb_dist, nb_unitvec, ava_idx
                            torch.cuda.empty_cache()
                            handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                            meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            p1 = p
                            p = p + int(meminfo.free/(a2-a1))
                        if p >= len(idx):
                            ava_idx = torch.tensor(idx[p1:].astype(int)).to(self.par.device)
                            nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                            if self.cutoff is not None:
                                nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                )
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                            del nb_dist, nb_unitvec, ava_idx
                            torch.cuda.empty_cache()
                        pynvml.nvmlShutdown()
                ffile.close()

            if self.ava_idx != None and self.ava_idx.device != torch.device(self.par.device): #cuda 0 by default
                if self.require_distances and len(self.ava_idx):
                    import pynvml 
                    pynvml.nvmlInit()
                    p = 0
                    p1 = 0
                    a1 = torch.cuda.memory_allocated()
                    asingle = torch.tensor([1,1]).to(self.par.device)
                    a2 = torch.cuda.memory_allocated()
                    d = asingle.get_device()
                    handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                    meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    p = int(meminfo.free*0.8/(a2-a1))
                    if itstep != None and self.cutoff != None:
                        if reconstep == None:
                            reconstep = 10 #reconstep is 10 by default
                        if reconstep <= 1:
                            raise ValueError(" reconstep can not less than 2")
                        if itstep % reconstep == 0:
                            while p < len(self.ava_idx):
                                self.neighborlist = torch.tensor([[]]*2, dtype=int).T.to(self.par.device)
                                ava_idx = self.ava_idx[p1:p].to(self.par.device)
                                nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                if delt_r == None:
                                    delt_r = self.cutoff
                                _, _, vl = self._neighbor_verlet_list(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                )
                                self.neighborlist = torch.cat((self.neighborlist,vl), axis = 0)
                                torch.cuda.empty_cache()
                                handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                                meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                                p1 = p
                                p = p + int(meminfo.free/(a2-a1))

                            if p >= len(self.ava_idx):
                                ava_idx = self.ava_idx[p1:].to(self.par.device)
                                nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                _, _, vl = self._neighbor_verlet_list(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                )
                                self.neighborlist = torch.cat((self.neighborlist,vl), axis = 0)
                        
                        if self.neighborlist == None:
                            raise ValueError("itration step should start from 0")
                        nbv_dist, nbv_unitvec, _ = calculate_distances(spos, self.neighborlist, sbox)
                        nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                            nbv_dist, (nbv_dist, nbv_unitvec, self.neighborlist)
                        )
                        for v in self.energies:
                            if v == "electrostatics":
                                E, force_coeff = evaluate_electrostatics(
                                    nb_dist,
                                    ava_idx,
                                    self.par.charges,
                                    cutoff=self.cutoff,
                                    rfa=self.rfa,
                                    solventDielectric=self.solventDielectric,
                                    explicit_forces=explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            elif v == "lj":
                                E, force_coeff = evaluate_LJ(
                                    nb_dist,
                                    ava_idx,
                                    self.par.mapped_atom_types,
                                    self.par.A,
                                    self.par.B,
                                    self.switch_dist,
                                    self.cutoff,
                                    explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            elif v == "repulsion":
                                E, force_coeff = evaluate_repulsion(
                                    nb_dist,
                                    ava_idx,
                                    self.par.mapped_atom_types,
                                    self.par.A,
                                    explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            elif v == "repulsioncg":
                                E, force_coeff = evaluate_repulsion_CG(
                                    nb_dist,
                                    ava_idx,
                                    self.par.mapped_atom_types,
                                    self.par.B,
                                    explicit_forces,
                                )
                                pot[i][v] += E.sum()
                            else:
                                continue
                            
                            if explicit_forces:
                                forcevec = nb_unitvec * force_coeff[:, None]
                                forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                        del nb_dist, nb_unitvec, ava_idx
                        torch.cuda.empty_cache()
                        pynvml.nvmlShutdown()
                    else:
                        while p < len(self.ava_idx):
                            ava_idx = self.ava_idx[p1:p].to(self.par.device)
                            nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                            if self.cutoff is not None:
                                nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                )
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                            del nb_dist, nb_unitvec, ava_idx
                            torch.cuda.empty_cache()
                            handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                            meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            p1 = p
                            p = p + int(meminfo.free*0.8/(a2-a1))
                        if p >= len(self.ava_idx):
                            ava_idx = self.ava_idx[p1:].to(self.par.device)
                            nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                            if self.cutoff is not None:
                                nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                )
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                            del nb_dist, nb_unitvec, ava_idx
                            torch.cuda.empty_cache()
                            pynvml.nvmlShutdown()
                    
#breakpoint to monitor the cuda memory                            print(torch.cuda.memory_reserved(),'a')
            if self.ava_idx != None and self.ava_idx.device == torch.device(self.par.device):
                if self.require_distances and len(self.ava_idx):
                    try:
                        nb_dist, nb_unitvec, _ = calculate_distances(spos, self.ava_idx, sbox)
                        ava_idx = self.ava_idx
                        if itstep != None and self.cutoff != None:
                            if reconstep == None:
                                reconstep = 10 #reconstep is 10 by default
                            if reconstep <= 1:
                                raise ValueError(" reconstep can not less than 2")
                            if itstep % reconstep == 0:
                                if delt_r == None:
                                    delt_r = self.cutoff
                                nb_dist, nb_unitvec, self.neighborlist  = self._neighbor_verlet_list(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                )
                            if self.neighborlist == None:
                                raise ValueError("itration step should start from 0")
                            nbv_dist, nbv_unitvec, _ = calculate_distances(spos, self.neighborlist, sbox)
                            nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                nbv_dist, (nbv_dist, nbv_unitvec, self.neighborlist)
                            )
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                        else:
                            if self.cutoff is not None:
                                nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                    nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                )
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                        del nb_dist, nb_unitvec, ava_idx
                        torch.cuda.empty_cache()
                    except RuntimeError:
                        print('Go to the RuntimeError part')
                        import pynvml 
                        pynvml.nvmlInit()
                        p = 0
                        p1 = 0
                        a1 = torch.cuda.memory_allocated()
                        asingle = torch.tensor([1,1]).to(self.par.device)
                        a2 = torch.cuda.memory_allocated()
                        d = asingle.get_device()
                        handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                        meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                        p = int(meminfo.free*0.8/(a2-a1))
                        if itstep != None and self.cutoff != None:
                            if reconstep == None:
                                reconstep = 10 #reconstep is 10 by default
                            if reconstep <= 1:
                                raise ValueError(" reconstep can not less than 2")
                            if itstep % reconstep == 0:
                                while p < len(self.ava_idx):
                                    self.neighborlist = torch.tensor([[]]*2, dtype=int).T.to(self.par.device)
                                    ava_idx = self.ava_idx[p1:p]
                                    nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                    if delt_r == None:
                                        delt_r = self.cutoff
                                    _, _, vl = self._neighbor_verlet_list(
                                        nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                    )
                                    self.neighborlist = torch.cat((self.neighborlist,vl), axis = 0)
                                    torch.cuda.empty_cache()
                                    handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                                    meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                                    p1 = p
                                    p = p + int(meminfo.free/(a2-a1))
                                if p >= len(self.ava_idx):
                                    ava_idx = self.ava_idx[p1:]
                                    nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                    _, _, vl = self._neighbor_verlet_list(
                                        nb_dist, (nb_dist, nb_unitvec, ava_idx), delt_r
                                    )
                                    self.neighborlist = torch.cat((self.neighborlist,vl), axis = 0)
                            if self.neighborlist == None:
                                raise ValueError("itration step should start from 0")
                            nbv_dist, nbv_unitvec, _ = calculate_distances(spos, self.neighborlist, sbox)
                            nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                nbv_dist, (nbv_dist, nbv_unitvec, self.neighborlist)
                            )
                            for v in self.energies:
                                if v == "electrostatics":
                                    E, force_coeff = evaluate_electrostatics(
                                        nb_dist,
                                        ava_idx,
                                        self.par.charges,
                                        cutoff=self.cutoff,
                                        rfa=self.rfa,
                                        solventDielectric=self.solventDielectric,
                                        explicit_forces=explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "lj":
                                    E, force_coeff = evaluate_LJ(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        self.par.B,
                                        self.switch_dist,
                                        self.cutoff,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsion":
                                    E, force_coeff = evaluate_repulsion(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.A,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                elif v == "repulsioncg":
                                    E, force_coeff = evaluate_repulsion_CG(
                                        nb_dist,
                                        ava_idx,
                                        self.par.mapped_atom_types,
                                        self.par.B,
                                        explicit_forces,
                                    )
                                    pot[i][v] += E.sum()
                                else:
                                    continue
                                
                                if explicit_forces:
                                    forcevec = nb_unitvec * force_coeff[:, None]
                                    forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                    forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                        else:
                            while p < len(self.ava_idx):
                                ava_idx = self.ava_idx[p1:p]
                                nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                if self.cutoff is not None:
                                    nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                        nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                    )

                                for v in self.energies:
                                    if v == "electrostatics":
                                        E, force_coeff = evaluate_electrostatics(
                                            nb_dist,
                                            ava_idx,
                                            self.par.charges,
                                            cutoff=self.cutoff,
                                            rfa=self.rfa,
                                            solventDielectric=self.solventDielectric,
                                            explicit_forces=explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    elif v == "lj":
                                        E, force_coeff = evaluate_LJ(
                                            nb_dist,
                                            ava_idx,
                                            self.par.mapped_atom_types,
                                            self.par.A,
                                            self.par.B,
                                            self.switch_dist,
                                            self.cutoff,
                                            explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    elif v == "repulsion":
                                        E, force_coeff = evaluate_repulsion(
                                            nb_dist,
                                            ava_idx,
                                            self.par.mapped_atom_types,
                                            self.par.A,
                                            explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    elif v == "repulsioncg":
                                        E, force_coeff = evaluate_repulsion_CG(
                                            nb_dist,
                                            ava_idx,
                                            self.par.mapped_atom_types,
                                            self.par.B,
                                            explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    else:
                                        continue
                                    
                                    if explicit_forces:
                                        forcevec = nb_unitvec * force_coeff[:, None]
                                        forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                        forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                                del nb_dist, nb_unitvec, ava_idx
                                torch.cuda.empty_cache()
                                handle = pynvml.nvmlDeviceGetHandleByIndex(d)
                                meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                                p1 = p
                                p = p + int(meminfo.free*0.8/(a2-a1))
                            if p >= len(self.ava_idx):
                                ava_idx = self.ava_idx[p1:].to(self.par.device)
                                nb_dist, nb_unitvec, _ = calculate_distances(spos, ava_idx, sbox)
                                if self.cutoff is not None:
                                    nb_dist, nb_unitvec, ava_idx = self._filter_by_cutoff(
                                        nb_dist, (nb_dist, nb_unitvec, ava_idx)
                                    )
                                for v in self.energies:
                                    if v == "electrostatics":
                                        E, force_coeff = evaluate_electrostatics(
                                            nb_dist,
                                            ava_idx,
                                            self.par.charges,
                                            cutoff=self.cutoff,
                                            rfa=self.rfa,
                                            solventDielectric=self.solventDielectric,
                                            explicit_forces=explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    elif v == "lj":
                                        E, force_coeff = evaluate_LJ(
                                            nb_dist,
                                            ava_idx,
                                            self.par.mapped_atom_types,
                                            self.par.A,
                                            self.par.B,
                                            self.switch_dist,
                                            self.cutoff,
                                            explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    elif v == "repulsion":
                                        E, force_coeff = evaluate_repulsion(
                                            nb_dist,
                                            ava_idx,
                                            self.par.mapped_atom_types,
                                            self.par.A,
                                            explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    elif v == "repulsioncg":
                                        E, force_coeff = evaluate_repulsion_CG(
                                            nb_dist,
                                            ava_idx,
                                            self.par.mapped_atom_types,
                                            self.par.B,
                                            explicit_forces,
                                        )
                                        pot[i][v] += E.sum()
                                    else:
                                        continue
                                    
                                    if explicit_forces:
                                        forcevec = nb_unitvec * force_coeff[:, None]
                                        forces[i].index_add_(0, ava_idx[:, 0], -forcevec)
                                        forces[i].index_add_(0, ava_idx[:, 1], forcevec)
                                del nb_dist, nb_unitvec, ava_idx
                                torch.cuda.empty_cache()
                                pynvml.nvmlShutdown()
                    
        if self.external:
            ext_ene, ext_force = self.external.calculate(pos, box)
            for s in range(nsystems):
                pot[s]["external"] += ext_ene[s]
            if explicit_forces:
                forces += ext_force
        
        if itstep != None:
            if not explicit_forces:
                enesum = torch.zeros(1, device=pos.device, dtype=pos.dtype)
                for i in range(nsystems):
                    for ene in pot[i]:
                        if pot[i][ene].requires_grad:
                            enesum += pot[i][ene]
                forces[:] = -torch.autograd.grad(
                    enesum, pos, only_inputs=True, retain_graph=True
                )[0]
                if returnDetails:
                    return pot, self.neighborlist
                else:
                    return [torch.sum(torch.cat(list(pp.values()))) for pp in pot], self.neighborlist

            if returnDetails:
                return [{k: v.cpu().item() for k, v in pp.items()} for pp in pot], self.neighborlist
            else:
                return [np.sum([v.cpu().item() for _, v in pp.items()]) for pp in pot], self.neighborlist
        
        else:
            if not explicit_forces:
                enesum = torch.zeros(1, device=pos.device, dtype=pos.dtype)
                for i in range(nsystems):
                    for ene in pot[i]:
                        if pot[i][ene].requires_grad:
                            enesum += pot[i][ene]
                forces[:] = -torch.autograd.grad(
                    enesum, pos, only_inputs=True, retain_graph=True
                )[0]
                if returnDetails:
                    return pot
                else:
                    return [torch.sum(torch.cat(list(pp.values()))) for pp in pot]

            if returnDetails:
                return [{k: v.cpu().item() for k, v in pp.items()} for pp in pot]
            else:
                return [np.sum([v.cpu().item() for _, v in pp.items()]) for pp in pot]

    def _make_indeces(self, natoms, excludepairs, device):
#if cpu memory is larger than the gpu's
        ava_idx = None
        l_excludepairs = len(excludepairs)
        try:
            fmatrix = np.full((natoms, natoms), True, dtype=bool)
            if l_excludepairs:
                excludepairs = np.array(excludepairs)
                fmatrix[excludepairs[:, 0], excludepairs[:, 1]] = False
                fmatrix[excludepairs[:, 1], excludepairs[:, 0]] = False
            np.where(fmatrix) #check the available memory 
            fmatrix = np.triu(fmatrix, +1)
            ava_idx_i = np.vstack(np.where(fmatrix)).T
#breakpoint            print(torch.cuda.memory_reserved(),'a')
            try:
                ava_idx = torch.tensor(ava_idx_i).to(device)
            except RuntimeError:
                print('cuda is out of memory but the internal memory of cpu is enough')
                torch.cuda.empty_cache()
                #Solution: turn ava_idx to be stored in the cpu
                ava_idx = torch.tensor(ava_idx_i).to('cpu')
        except MemoryError:
            print('both cpu and gpu are out of memory')
            #Solution-2: We put the data to the outside(external memory).
            if os.path.exists('non-interactions.h5') != True:
                filters = t.Filters(complevel=5,complib='blosc')
                ffile = t.open_file('non-interactions.h5', mode = 'w', title = 'index')
                import psutil
                import sys
                singlesize = sys.getsizeof(np.full((1,1),True,dtype=bool))
                length = int(psutil.virtual_memory().free/singlesize/natoms)
                length_i = length
                m = 0
                length0 = 0
                ava = np.array([[]]*2).T
#breakpoint                print(psutil.virtual_memory(),'1')
                i = 0
                earray = ffile.create_earray(ffile.root, 'data', atom=t.Atom.from_dtype(ava.dtype), shape=(0,2), filters=filters, expectedrows=int(natoms*natoms*0.8))
                if l_excludepairs:
                    excludepairs = np.array(excludepairs)
                    ex_index = np.lexsort((excludepairs[:,1],excludepairs[:,0]))
                    excludepairs = excludepairs[ex_index]
                while length <= natoms :
                    fmatrix = np.full((length_i, natoms), True, dtype=bool)
                    if l_excludepairs and m < l_excludepairs-1:
                        for j in range(m, l_excludepairs):
                            if excludepairs[j,0] >= length:
                                m1 = m
                                m = j
                                break
                            else:
                                if j == l_excludepairs-1:
                                    m1 = m
                                    m = j+1
                        excludepairs_i = excludepairs[m1:m]
                        fmatrix[excludepairs_i[:, 0]-length0, excludepairs_i[:, 1]] = False
#breakpoint                    print(i) 
#breakpoint                    print(excludepairs_i)
                    fmatrix = np.triu(fmatrix, length0+1)
                    allvsall_indeces = np.vstack(np.where(fmatrix)).T
                    allvsall_indeces = np.vstack((allvsall_indeces[:,0]+length0,allvsall_indeces[:,1])).T
                    allvsall_indeces = allvsall_indeces.astype(int)
                    earray.append(allvsall_indeces)
                    i += 1
                    length_i = int(psutil.virtual_memory().free/singlesize/natoms)
                    length0 = length
                    length = length + length_i
#breakpoint                    print(len(allvsall_indeces),'**')
                if length > natoms:
                    fmatrix = np.full((natoms - length0, natoms), True, dtype=bool)
                    if l_excludepairs and m < l_excludepairs:
                        excludepairs_i = excludepairs[m:]
                        fmatrix[excludepairs_i[:, 0] - length0, excludepairs_i[:, 1]] = False
                    fmatrix = np.triu(fmatrix, length0+1)
                    allvsall_indeces = np.vstack(np.where(fmatrix)).T
                    allvsall_indeces = np.vstack((allvsall_indeces[:,0]+length0,allvsall_indeces[:,1])).T
                    allvsall_indeces = allvsall_indeces.astype(int)
                    earray.append(allvsall_indeces)
                ffile.close()
        return ava_idx


def wrap_dist(dist, box):
    if box is None or torch.all(box == 0):
        wdist = dist
    else:
        wdist = dist - box.unsqueeze(0) * torch.round(dist / box.unsqueeze(0))
    return wdist


def calculate_distances(atom_pos, atom_idx, box):
    direction_vec = wrap_dist(atom_pos[atom_idx[:, 0]] - atom_pos[atom_idx[:, 1]], box)
    dist = torch.norm(direction_vec, dim=1)
    direction_unitvec = direction_vec / dist.unsqueeze(1)
    return dist, direction_unitvec, direction_vec


ELEC_FACTOR = 1 / (4 * const.pi * const.epsilon_0)  # Coulomb's constant
ELEC_FACTOR *= const.elementary_charge ** 2  # Convert elementary charges to Coulombs
ELEC_FACTOR /= const.angstrom  # Convert Angstroms to meters
ELEC_FACTOR *= const.Avogadro / (const.kilo * const.calorie)  # Convert J to kcal/mol


def evaluate_LJ(
    dist, pair_indeces, atom_types, A, B, switch_dist, cutoff, explicit_forces=True
):
    atomtype_indices = atom_types[pair_indeces]
    aa = A[atomtype_indices[:, 0], atomtype_indices[:, 1]]
    bb = B[atomtype_indices[:, 0], atomtype_indices[:, 1]]
    return evaluate_LJ_internal(dist, aa, bb, 1, switch_dist, cutoff, explicit_forces)


def evaluate_LJ_internal(
    dist, aa, bb, scale, switch_dist, cutoff, explicit_forces=True
):
    force = None

    rinv1 = 1 / dist
    rinv6 = rinv1 ** 6
    rinv12 = rinv6 * rinv6

    pot = ((aa * rinv12) - (bb * rinv6)) / scale
    if explicit_forces:
        force = (-12 * aa * rinv12 + 6 * bb * rinv6) * rinv1 / scale

    # Switching function
    if switch_dist is not None and cutoff is not None:
        mask = dist > switch_dist
        t = (dist[mask] - switch_dist) / (cutoff - switch_dist)
        switch_val = 1 + t * t * t * (-10 + t * (15 - t * 6))
        if explicit_forces:
            switch_deriv = t * t * (-30 + t * (60 - t * 30)) / (cutoff - switch_dist)
            force[mask] = (
                switch_val * force[mask] + pot[mask] * switch_deriv / dist[mask]
            )
        pot[mask] = pot[mask] * switch_val

    return pot, force


def evaluate_repulsion(
    dist, pair_indeces, atom_types, A, scale=1, explicit_forces=True
):  # LJ without B
    force = None

    atomtype_indices = atom_types[pair_indeces]
    aa = A[atomtype_indices[:, 0], atomtype_indices[:, 1]]

    rinv1 = 1 / dist
    rinv6 = rinv1 ** 6
    rinv12 = rinv6 * rinv6

    pot = (aa * rinv12) / scale
    if explicit_forces:
        force = (-12 * aa * rinv12) * rinv1 / scale
    return pot, force


def evaluate_repulsion_CG(
    dist, pair_indeces, atom_types, B, scale=1, explicit_forces=True
):  # Repulsion like from CGNet
    force = None

    atomtype_indices = atom_types[pair_indeces]
    coef = B[atomtype_indices[:, 0], atomtype_indices[:, 1]]

    rinv1 = 1 / dist
    rinv6 = rinv1 ** 6

    pot = (coef * rinv6) / scale
    if explicit_forces:
        force = (-6 * coef * rinv6) * rinv1 / scale
    return pot, force


def evaluate_electrostatics(
    dist,
    pair_indeces,
    atom_charges,
    scale=1,
    cutoff=None,
    rfa=False,
    solventDielectric=78.5,
    explicit_forces=True,
):
    force = None
    if rfa:  # Reaction field approximation for electrostatics with cutoff
        # http://docs.openmm.org/latest/userguide/theory.html#coulomb-interaction-with-cutoff
        # Ilario G. Tironi, René Sperb, Paul E. Smith, and Wilfred F. van Gunsteren. A generalized reaction field method
        # for molecular dynamics simulations. Journal of Chemical Physics, 102(13):5451–5459, 1995.
        denom = (2 * solventDielectric) + 1
        krf = (1 / cutoff ** 3) * (solventDielectric - 1) / denom
        crf = (1 / cutoff) * (3 * solventDielectric) / denom
        common = (
            ELEC_FACTOR
            * atom_charges[pair_indeces[:, 0]]
            * atom_charges[pair_indeces[:, 1]]
            / scale
        )
        dist2 = dist ** 2
        pot = common * ((1 / dist) + krf * dist2 - crf)
        if explicit_forces:
            force = common * (2 * krf * dist - 1 / dist2)
    else:
        pot = (
            ELEC_FACTOR
            * atom_charges[pair_indeces[:, 0]]
            * atom_charges[pair_indeces[:, 1]]
            / dist
            / scale
        )
        if explicit_forces:
            force = -pot / dist
    return pot, force


def evaluate_bonds(dist, bond_params, explicit_forces=True):
    force = None

    k0 = bond_params[:, 0]
    d0 = bond_params[:, 1]
    x = dist - d0
    pot = k0 * (x ** 2)
    if explicit_forces:
        force = 2 * k0 * x
    return pot, force


def evaluate_angles(r21, r23, angle_params, explicit_forces=True):
    k0 = angle_params[:, 0]
    theta0 = angle_params[:, 1]

    dotprod = torch.sum(r23 * r21, dim=1)
    norm23inv = 1 / torch.norm(r23, dim=1)
    norm21inv = 1 / torch.norm(r21, dim=1)

    cos_theta = dotprod * norm21inv * norm23inv
    cos_theta = torch.clamp(cos_theta, -1, 1)
    theta = torch.acos(cos_theta)

    delta_theta = theta - theta0
    pot = k0 * delta_theta * delta_theta

    force0, force1, force2 = None, None, None
    if explicit_forces:
        sin_theta = torch.sqrt(1.0 - cos_theta * cos_theta)
        coef = torch.zeros_like(sin_theta)
        nonzero = sin_theta != 0
        coef[nonzero] = -2.0 * k0[nonzero] * delta_theta[nonzero] / sin_theta[nonzero]
        force0 = (
            coef[:, None]
            * (cos_theta[:, None] * r21 * norm21inv[:, None] - r23 * norm23inv[:, None])
            * norm21inv[:, None]
        )
        force2 = (
            coef[:, None]
            * (cos_theta[:, None] * r23 * norm23inv[:, None] - r21 * norm21inv[:, None])
            * norm23inv[:, None]
        )
        force1 = -(force0 + force2)

    return pot, (force0, force1, force2)


def evaluate_torsion(r12, r23, r34, torsion_params, explicit_forces=True):
    # Calculate dihedral angles from vectors
    crossA = torch.cross(r12, r23, dim=1)
    crossB = torch.cross(r23, r34, dim=1)
    crossC = torch.cross(r23, crossA, dim=1)
    normA = torch.norm(crossA, dim=1)
    normB = torch.norm(crossB, dim=1)
    normC = torch.norm(crossC, dim=1)
    normcrossB = crossB / normB.unsqueeze(1)
    cosPhi = torch.sum(crossA * normcrossB, dim=1) / normA
    sinPhi = torch.sum(crossC * normcrossB, dim=1) / normC
    phi = -torch.atan2(sinPhi, cosPhi)

    ntorsions = len(torsion_params[0]["idx"])
    pot = torch.zeros(ntorsions, dtype=r12.dtype, layout=r12.layout, device=r12.device)
    if explicit_forces:
        coeff = torch.zeros(
            ntorsions, dtype=r12.dtype, layout=r12.layout, device=r12.device
        )
    for i in range(0, len(torsion_params)):
        idx = torsion_params[i]["idx"]
        k0 = torsion_params[i]["params"][:, 0]
        phi0 = torsion_params[i]["params"][:, 1]
        per = torsion_params[i]["params"][:, 2]

        if torch.all(per > 0):  # AMBER torsions
            angleDiff = per * phi[idx] - phi0
            pot.scatter_add_(0, idx, k0 * (1 + torch.cos(angleDiff)))
            if explicit_forces:
                coeff.scatter_add_(0, idx, -per * k0 * torch.sin(angleDiff))
        else:  # CHARMM torsions
            angleDiff = phi[idx] - phi0
            angleDiff[angleDiff < -pi] = angleDiff[angleDiff < -pi] + 2 * pi
            angleDiff[angleDiff > pi] = angleDiff[angleDiff > pi] - 2 * pi
            pot.scatter_add_(0, idx, k0 * angleDiff ** 2)
            if explicit_forces:
                coeff.scatter_add_(0, idx, 2 * k0 * angleDiff)

    # coeff.unsqueeze_(1)

    force0, force1, force2, force3 = None, None, None, None
    if explicit_forces:
        # Taken from OpenMM
        normDelta2 = torch.norm(r23, dim=1)
        norm2Delta2 = normDelta2 ** 2
        forceFactor0 = (-coeff * normDelta2) / (normA ** 2)
        forceFactor1 = torch.sum(r12 * r23, dim=1) / norm2Delta2
        forceFactor2 = torch.sum(r34 * r23, dim=1) / norm2Delta2
        forceFactor3 = (coeff * normDelta2) / (normB ** 2)

        force0vec = forceFactor0.unsqueeze(1) * crossA
        force3vec = forceFactor3.unsqueeze(1) * crossB
        s = (
            forceFactor1.unsqueeze(1) * force0vec
            - forceFactor2.unsqueeze(1) * force3vec
        )

        force0 = -force0vec
        force1 = force0vec + s
        force2 = force3vec - s
        force3 = -force3vec

    return pot, (force0, force1, force2, force3)

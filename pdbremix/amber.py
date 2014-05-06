# encoding: utf-8

__doc__ = """

PDBREMIX interface to the AMBER molecular-dynamics package.

The library is split into three sections:

1. Read and write restart files
2. Generate restart files from PDB
3. Run simulations from restart files
4. Read trajectories with some post-processing

Copyright (C) 2009, 2014, Bosco K. Ho
"""


import os
import copy
import shutil
import re

import util
import v3
import pdbtext
import pdbatoms
import data
import protein


# ##########################################################

# 1. Reading and writing restart files

# In PDBREMIX, restart files for AMBER are assumed to
# have the naming scheme:

# 1. topology file: sim.top
# 2. coordinate/velocity file: sim.crd(coor) or sim.rst (coor/vel)

# Parsers have been written to read .top and .crd/.rst files into
# Python structures, and to write these back into .top and .crd/.rst
# files, and to convert them into .pdb files

# The units used in AMBER are:
# - positions: angs
# - velocities: angs/ps/20.455 
# - force constant: kcal/mol/ang^2


def read_top(top):
  """
  Returns a topology dictionary containing all the fields in the
  AMBER .top file referenced by their FLAG name, and formatted into
  python data types. POINTER variables are given their own key-value
  fields.
  """
  section = None
  len_field = None
  parse = None
  parse_map = { 'a':str, 'I':int, 'E':float }
  topology = {}
  for line in open(top, "rU"):
    line = line[:-1]
    if line.startswith("%"):
      words = line.split()
      key = words[0][1:]
      if key == "FLAG":
        section = words[1]
        topology[section] = []
      elif key.startswith("FORMAT"):
        # interprets FORTRAN string format to parse section
        format_str = key[7:-1]
        len_field = int(re.split(r'\D+', format_str)[1])
        val_type = re.search('(a|I|E)', format_str).group(0)
        parse = parse_map[val_type]
    else:
      indices = range(0, len(line), len_field)
      pieces = [line[i:i+len_field] for i in indices]
      topology[section].extend(map(parse, pieces))
  name_str = """
  NATOM NTYPES NBONH MBONA NTHETH 
  MTHETA NPHIH MPHIA NHPARM NPARM 
  NNB NRES NBONA NTHETA NPHIA 
  NUMBND NUMANG NPTRA NATYP NPHB 
  IFPERT NBPER NGPER NDPER MBPER 
  MGPER MDPER IFBOX NMXRS IFCAP
  NUMEXTRA NCOPY  """
  for name, val in zip(name_str.split(), topology['POINTERS']):
    topology[name] = val
  return topology


def convert_to_pdb_atom_names(soup):
  for res in soup.residues():
    if res.type in data.solvent_res_types:
      for a in res.atoms():
        a.is_hetatm = True
    if res.type == "HSE":
      res.set_type("HIS")
    if res.type == "HIE":
      res.set_type("HIS")
    if res.type == "CYX":
      res.set_type("CYS")
    for atom in res.atoms():
      if atom.type[-1].isdigit() and atom.type[0] == "H":
        new_atom_type = atom.type[-1] + atom.type[:-1]
        res.change_atom_type(atom.type, new_atom_type)
      if atom.res_type == data.solvent_res_types:
        atom.is_hetatm = True


def soup_from_topology(topology):
  """
  Returns a PDBREMIX soup object from a topology dictionary.
  """
  soup = pdbatoms.Polymer()
  chain_id = ''
  n_res = topology['NRES']
  n_atom = topology['NATOM']
  for i_res in range(n_res):
    res_type = topology['RESIDUE_LABEL'][i_res].strip()
    if res_type == "WAT":
      res_type = "HOH"
    res = pdbatoms.Residue(res_type, chain_id, i_res+1)
    soup.append_residue(res)
    res = soup.residue(i_res)
    i_atom_start = topology['RESIDUE_POINTER'][i_res] - 1
    if i_res == n_res-1:
      i_atom_end = n_atom
    else:
      i_atom_end = topology['RESIDUE_POINTER'][i_res+1] - 1
    for i_atom in range(i_atom_start, i_atom_end):
      atom = pdbatoms.Atom()
      atom.vel = v3.vector()
      atom.num = i_atom+1
      atom.res_num = i_res+1
      atom.res_type = res_type
      atom.type = topology['ATOM_NAME'][i_atom].strip()
      atom.mass = topology['MASS'][i_atom]
      atom.charge = topology['CHARGE'][i_atom]/sqrt_of_k
      atom.element = pdbatoms.guess_element(
          atom.res_type, atom.type)
      soup.insert_atom(-1, atom)
  protein.find_chains(soup)
  convert_to_pdb_atom_names(soup)
  return soup


def load_crd_or_rst_into_soup(soup, crd_or_rst):
  """
  Loads the coordinates and velocities of .crd or .rst into the soup.
  """
  f = open(crd_or_rst, "r")
  
  f.readline() # skip first line
  n_atom = int(f.readline().split()[0])

  # calculate size of file based on field sizes
  n_crd = n_atom * 3
  n_line = n_crd / 6
  if n_crd % 6 > 0:
    n_line += 1

  # read all the numbers in the coordinate section
  line_list = [f.readline()[:-1] for i in range(0, n_line)]
  s = "".join(line_list)
  vals = [float(s[i:i+12]) for i in xrange(0, len(s), 12)]
  if len(vals) != n_crd:
    raise ValueError, "Improper number of coordinates in rst file."

  # load numbers into soup object  
  for i, atom in enumerate(sorted(soup.atoms(), pdbatoms.cmp_atom)):
    v3.set_vector(atom.pos, vals[i*3], vals[i*3+1], vals[i*3+2])

  # if .rst file, then there will be velocity values
  if crd_or_rst.endswith('.rst'):
    line_list = [f.readline()[:-1] for i in range(0, n_line)]
    s = "".join(line_list)
    vals = [float(s[i:i+12]) for i in xrange(0, len(s), 12)]
    if len(vals) != n_crd:
      raise ValueError, "Improper number of coordinates in rst file."

    # now convert amber velocities to angs/ps and load into soup
    convert_vel_to_angs_per_ps = 20.455
    for i, atom in enumerate(sorted(soup.atoms(), pdbatoms.cmp_atom)):
      v3.set_vector(atom.vel, vals[i*3], vals[i*3+1], vals[i*3+2])
      atom.vel = v3.scale(atom.vel, convert_vel_to_angs_per_ps)

  f.close()


def soup_from_top_and_crd_or_rst(top, crd_or_rst):
  """
  Returns a soup object from AMBER .top and .crd/.rst files.
  """
  topology = read_top(top)
  soup = soup_from_topology(topology)
  load_crd_or_rst_into_soup(soup, crd_or_rst)
  if topology['IFBOX'] > 0:
    # if periodic cells are in .crd or .rst then save
    # for later, if we need to write modified .crd or .rst 
    lines = open(crd_or_rst, "r").readlines()
    lines = [l for l in reversed(lines) if l.strip()]
    soup.box_dimension_str = lines[0].rstrip()
  return soup


def write_soup_to_rst(soup, rst):
  """
  Writes a .rst file mainly for pulsing simulations.
  """
  f = open(rst, "w")

  # header with number of atoms in first row
  f.write(" ".ljust(80) + "\n")
  f.write("%5d  0.0000000E+00\n" % len(soup.atoms()))

  # write coordinates
  i = 0
  for atom in sorted(soup.atoms(), pdbatoms.cmp_atom):
    x, y, z = atom.pos
    f.write("%12.7f%12.7f%12.7f" % (x, y, z))
    i += 1
    if i % 2 == 0:
      f.write("\n")
      i = 0
  if len(soup.atoms()) % 2 != 0:
    f.write("\n")

  # write velocities
  i = 0
  convert_to_amber_vel = 1.0 / 20.455
  for atom in sorted(soup.atoms(), pdbatoms.cmp_atom):
    x, y, z = atom.vel
    vx = x * convert_to_amber_vel
    vy = y * convert_to_amber_vel
    vz = z * convert_to_amber_vel
    f.write("%12.7f%12.7f%12.7f" % (vx, vy, vz))
    i += 1
    if i % 2 == 0:
      f.write("\n")
  if len(soup.atoms()) % 2 != 0:
    f.write("\n")

  # write box dimensions
  if hasattr(soup, 'box_dimension_str'):
    if soup.box_dimension_str:
      f.write(soup.box_dimension_str.rstrip() + "\n")
    
  f.close()
  
  
# The following functions wrap the above functions into a
# standard API that does not explicitly reference AMBER


def expand_restart_files(basename):
  """Returns expanded restart files based on basename"""
  top = os.path.abspath(basename + '.top')
  crds = os.path.abspath(basename + '.rst')
  if not os.path.isfile(crds):
    crds = os.path.abspath(basename + '.crd')
  vels = ''
  return top, crds, vels


def get_restart_files(basename):
  """Returns restart files only if they exist"""
  top, crds, vels = expand_restart_files(basename)
  util.check_files(top, crds)
  return top, crds, vels


def soup_from_restart_files(top, crds, vels):
  """Reads pdbatoms.Polymer object from restart files."""
  return soup_from_top_and_crd_or_rst(top, crds)


def write_soup_to_crds_and_vels(soup, basename):
  """From soup, writes out the coordinate/velocities, used for pulsing"""
  write_soup_to_rst(soup, basename + '.rst')
  return basename + '.rst', ''


def convert_restart_to_pdb(basename, pdb):
  """Converts restart files with basename into PDB file"""
  top, crds, vels = get_restart_files(basename)
  soup = soup_from_restart_files(top, crds, vels)
  soup.write_pdb(pdb)
  

# ##########################################################

# # 2. Generate restart files from PDB

# The restart files used for PDBREMIX assumes a consistent file naming. 
# For a given basename `sim`, the files are:
# 1. topology file: sim.top
# 2. coordinate/velocity file: sim.crd or sim.rst

# To generate a topology file from the PDB file:
# - handles multiple protein chains
# - hydrogens are removed and then regenerated by AMBER
# - disulfide bonds are identified by PDBREMIX and explicitly encoded
# - charged residue protonation states are auto-detected
# - explicit water in cubic box with 10.0 angstrom buffer
# - counterions to neutralize the system
# - AMBER8: ff96 force-field
# - AMBER11: ff99SB force-field

# Binaries used to generate restart files:
# 1. tleap

# for charges: 18.312 = sqrt(332) where Columb's law E=332*q*q
sqrt_of_k = 18.2223


force_field_script = """
# leaprc to generate AMBER topology and coordinate

# load in amber force field
source %(amber_ff)s

# use AMBER6 PB radii as we will use igb=1, gbparm=2
set default PBradii mbondi
"""

explicit_water_box_script = """
# add explicit waters
solvateBox pdb TIP3PBOX %(solvent_buffer)f iso
"""

save_and_quit_script = """
# save topology and coordinates
saveAmberParm pdb %(top)s %(crd)s
quit
"""


def disulfide_script_and_rename_cysteines(in_pdb, out_pdb):
  """
  Returns the tleap script for disulfide bonds in the in_pdb file.

  This function opens in_pdb in a soup object, and searches for
  CYS residues where the SG-SG distance < 3 angs. These residues
  are then renamed to CYX and written to out_pdb. The disulfide bonds
  are then returned in a .tleap script fragment.
  """
  soup = pdbatoms.Polymer(in_pdb)
  script = " # disulfide bonds\n"
  n = len(soup.residues())
  for i in range(n):
    for j in range(i+1, n):
      if soup.residue(i).type in 'CYS' and soup.residue(j).type in 'CYS':
        p1 = soup.residue(i).atom('SG').pos
        p2 = soup.residue(j).atom('SG').pos
        if v3.distance(p1, p2) < 3.0:
          soup.residue(i).set_type('CYX')
          soup.residue(j).set_type('CYX')
          script += "bond pdb.%d.SG pdb.%d.SG\n" % (i+1, j+1)
  soup.write_pdb(out_pdb)
  util.check_output(out_pdb)
  return script


def run_tleap(
    force_field, pdb, name, solvent_buffer=0.0, excess_charge=0): 
  """
  Generates AMBER topology and coordinate files from PDB.

  Depending on whether excess_charge is non-zero, will also generate
  counterions. If solvent_buffer is non-zero, will generate explicit
  waters, otherwise, no waters generated. No waters is used for
  implicit solvent simulations.
  """

  util.check_output(pdb)

  # Remove all but protein heavy atoms in a single clean conformation
  tleap_pdb = name + '.clean.pdb'
  pdbtext.clean_pdb(pdb, tleap_pdb)

  # The restart files to be generated
  top = name + '.top'
  crd = name + '.crd'

  # Dictionary to substitute into tleap scripts
  params = { 
    'top': top, 
    'crd': crd, 
    'pdb': tleap_pdb,
    'data_dir':data.data_dir,
    'solvent_buffer': solvent_buffer,
  }

  # use best force-field for the 2 versions of AMBER author has tested
  if 'AMBER11' in force_field:
    params['amber_ff'] = "leaprc.ff99SB"
  elif 'AMBER8' in force_field:
    params['amber_ff'] = "leaprc.ff96"
  else:
    raise Exception("Don't know which version of AMBER(8|11) to use.")

  # make the tleap input script
  script = force_field_script
  # check for a few non-standard residue that have been included 
  residues = [r.type for r in pdbatoms.Polymer(tleap_pdb).residues()]
  if 'PHD' in residues:
    leaprc = open("%s/phd.leaprc" % data.data_dir).read()
    script += leaprc
  if 'ZNB' in residues:
    leaprc = open("%s/znb.leaprc" % data.data_dir).read()
    script += leaprc
  script += "pdb = loadpdb %(pdb)s\n"
  script += disulfide_script_and_rename_cysteines(tleap_pdb, tleap_pdb)
  if 'GBSA' not in force_field:
    # Add explicit waters as not GBSA implicit solvent
    if excess_charge != 0:
      # Add script to add counterions, must specify + or -
      if excess_charge > 0:
        script += "addions pdb Cl- 0\n"
      else:
        script += "addions pdb Na+ 0\n"
    solvent_buffer = 10
    params['solvent_buffer'] = solvent_buffer
    script += explicit_water_box_script
  script += save_and_quit_script
  script = script % params

  # Now write script to input file
  tleap_in = name + ".tleap.in"
  open(tleap_in, "w").write(script)

  # Now run tleap with tleap_in
  data.binary('tleap', "-f "+tleap_in, name+'.tleap')

  # Check output is okay
  if os.path.isfile('leap.log'):
    os.rename('leap.log', name + '.tleap.log')
  util.check_output(name+'.tleap.log', ['FATAL'])
  util.check_output(top)
  util.check_output(crd)

  return top, crd
  

def pdb_to_top_and_crds(
    force_field, pdb, name, solvent_buffer=0.0): 
  """
  Converts a PDB file into AMBER topology and coordinate files,
  and fully converted PDB file. These constitute the restart files
  of an AMBER simulation.
  """
  # Generate topology files with explicitly zero excess_charge.
  # We will then check if system is charged or not
  top, crd = run_tleap(
      force_field, pdb, name, solvent_buffer, 0)

  # In implicit solvent, we don't need to worry so much about
  # counterions so will skip counterion generation, otherwise
  if 'GBSA' not in force_field:
    # Get the charge of the system
    charges = read_top(name+'.top')['CHARGE']
    charge = int(round(sum(charges)/sqrt_of_k))

    # If the system has an overall charge, rerun with excess_charge,
    # which will for tleap to generate counterions
    if charge != 0:
      top, crd = run_tleap(
          force_field, pdb, name, solvent_buffer, charge)

  # make a reference PDB for generating restraints and viewing
  convert_restart_to_pdb(name, name+'.pdb')
  return top, crd


# ##########################################################

# # 3. Run simulations from restart files

# Simulation approach for implicit solvent:
# - optional positional constraints: 100 kcal/mol/angs**2 
# - Langevin thermostat for constant temperature
# - Nose-Hoover barometer with flexible periodic box size

# Simulation approach for explict water: 
# - cubic periodic box 
# - optional positional restraints: 100 kcal/mol/angs**2
# - PME electrostatics on the periodic box
# - Langevin thermostat for constant temperature
# - Nose-Hoover barometer with flexible periodic box size

# Binaries used:
# 1. sander

# Files for trajectories:
# 1. coordinate trajectory: md.trj
# 2. velocitiy trajectory: md.vel.trj
# 3. restart coordinate/velocity: md.rst


minimization_parms = { 
  'topology' : 'in.top', 
  'input_crds' : 'in.crd', 
  'output_name' : 'min', 
  'force_field': 'GBSA',
  'restraint_pdb': '',
  'restraint_force': 100.0,
  'n_step_minimization' : 100, 
} 

constant_energy_parms = { 
  'topology' : 'in.top', 
  'input_crds' : 'in.crd', 
  'output_name' : 'md', 
  'force_field': 'GBSA',
  'restraint_pdb': '',
  'restraint_force': 100.0,
  'n_step_per_snapshot' : 5, 
  'n_step_dynamics' : 1000, 
} 

langevin_thermometer_parms = { 
  'topology' : 'in.top', 
  'input_crds' : 'in.crd', 
  'output_name' : 'md', 
  'force_field': 'GBSA',
  'restraint_pdb': '',
  'restraint_force': 100.0,
  'random_seed' : 2342, 
  'temp_thermometer' : 300.0, 
  'temp_initial': 0.0, # ignored if it is 0.0
  'n_step_per_snapshot' : 5, 
  'n_step_dynamics' : 1000, 
  'n_step_per_thermostat' : 100, 
} 


# frequent low energy calculation
sander_script = """
generated by amber.py
&cntrl
"""


# no periodicity, generatlized born, and surface area terms
gbsa_script = "  ntb = 0, igb = 2, gbsa = 1, cut = 12.0,"


# peridoicity/constant pressure, isotropic position scaling, no gb/sa
explicit_water_script = "  ntb = 2, ntp = 1, igb = 0, gbsa = 0, cut = 8.0,"


# 10 steps of steepest descent then conjugate gradient for rest of steps
minimization_script = """
  imin = 1, ntmin = 1, maxcyc = %(n_step_minimization)s, ncyc = 10,
"""


dynamics_script = """
  ntpr = %(n_step_per_snapshot)s, ntave = 0, ntwr = 500, iwrap = 0, ioutfm = 0,
  ntwx = %(n_step_per_snapshot)s, ntwv = %(n_step_per_snapshot)s, 
  ntwe = %(n_step_per_snapshot)s, 
  nstlim = %(n_step_dynamics)s, nscm = 50, dt = 0.001,
  nrespa = 1,
"""

# langevin thermometer
thermostat_script = """
  ntt = 3, gamma_ln = 5, temp0 = %(temp_thermometer)s, vlimit = 0.0,
  ig = %(random_seed)s, tempi = %(temp_initial)s, 
"""


def make_sander_input_file(parms):
  """
  Make Sander input script based on parms dictionary info.
  """
  script = sander_script % parms

  # all bonds to be simulated, no constraints
  script += "  ntf = 1, ntc = 1,\n"

  # restraints are included
  if parms['restraint_pdb']:
    script += "  ntr = 1,\n"

  # To check if system has explicit solvent, see if the input
  # coordinate file contains periodic box information. This 
  # requires a bit of heavy lifting to figure out.
  soup = soup_from_top_and_crd_or_rst(
      parms['topology'], parms['input_crds'])
  has_periodic_box = \
      hasattr(soup, 'box_dimension_str') and \
      soup.box_dimension_str
  if has_periodic_box:
    script += explicit_water_script
  else:
    script += gbsa_script

  if 'n_step_minimization' in parms:
    script += minimization_script 

  elif 'n_step_dynamics' in parms:
    if parms['input_crds'].endswith('.rst'):
      script += "  ntx = 5, irest = 1,\n"
    else:
      script += "  ntx = 1,\n"
    script += dynamics_script
    if 'temp_thermometer' in parms:
      script += thermostat_script

  else:
    raise Exception("Can't parse parameters to run")

  script += "&end\n"

  return script % parms


restraint_script = """FIND
* * S *
* * B *
* * 3 *
* * E *
SEARCH
"""

def make_restraint_script(pdb, force=100.0):
  """
  Generates sander input fragment that specifies the atoms
  that will be restrained.

  The function reads a PDB file that was generated from the 
  topology functions above, and uses the B-factor field B>0 to 
  determine which atom is to be restrained. The atoms will
  be restrained by a spring of force in kcal/mol/angs**2 
  """
  util.check_output(pdb)
  script = "Restrained atoms from %s\n" % pdb
  script += "%s\n" % force
  script += restraint_script
  for i, atom in enumerate(pdbatoms.AtomList(pdb).atoms()):
    if atom.bfactor > 0.0:
      script += "ATOM %d %d\n" % (i+1, i+1)
  script += "END\n"
  script += "END\n"
  return script


def run(in_parms):
  """
  Run a AMBER simulations using the PDBREMIX parms dictionary.
  """
  parms = copy.deepcopy(in_parms)
  basename = parms['output_name']

  # Copies across topology file
  input_top = parms['topology'] 
  util.check_files(input_top)
  new_top = basename + '.top'
  shutil.copy(input_top, new_top)

  # Copies over coordinate/velocity files
  input_crd = parms['input_crds']
  util.check_files(input_crd)
  if input_crd.endswith('.crd'): 
    new_crd = basename + '.in.crd'
  else:
    new_crd = basename + '.in.rst'
  shutil.copy(input_crd, new_crd)
  
  # Decide on type of output coordinate/velocity file
  if 'n_step_minimization' in parms:
    rst = basename + ".crd"
  else:
    rst = basename + ".rst"

  # Construct the long list of arguments for sander
  trj = basename + ".trj"
  vel_trj = basename + ".vel.trj"
  ene = basename + ".ene"
  inf = basename + ".inf"
  sander_out = basename + ".sander.out"
  sander_in = basename + ".sander.in"
  args = "-O -i %s -o %s -p %s -c %s -r %s -x %s -v %s -e %s -inf %s" \
          % (sander_in, sander_out, new_top, new_crd, rst, trj, vel_trj, ene, inf)

  # Make the input script
  script = make_sander_input_file(parms)

  # If positional restraints
  if parms['restraint_pdb']:
    # Generate the AMBER .crd file that stores the constrained coordinates
    pdb = parms['restraint_pdb']
    soup = pdbatoms.Polymer(pdb)
    ref_crd = basename + '.restraint.crd'
    write_soup_to_rst(soup, ref_crd)
    util.check_output(ref_crd)
    # Add the restraints .crd to the SANDER arguments
    args += " -ref %s" % ref_crd
    # Add the restraint forces and atom indices to the SANDER input file
    script += make_restraint_script(pdb, parms['restraint_force'])

  open(sander_in, "w").write(script)

  # Run the simulation
  data.binary('sander', args, basename)

  # Check if output is okay
  util.check_output(sander_out, ['FATAL'])
  top, crds, vels = get_restart_files(basename)
  util.check_output(top)
  util.check_output(crds)


def low_temperature_equilibrate(in_name, out_name, temperature):
  """
  Carries out low temperature heating with a relaxation step in
  between with no thermostat. 

  The relaxation step allows the system to avoid the energy spike
  in low-temperature constant energy implicit-solvent simulations.
  """
  top, crd, vels = get_restart_files(in_name)
  md_dirs = ['heat1', 'const2', 'heat3']

  util.goto_dir(md_dirs[0])
  parms = langevin_thermometer_parms.copy()
  parms.extend({
    'topology': top,
    'input_crds': crd,
    'output_name': 'md',
    'temp_thermometer': temperature,
    'temp_initial': temperature,
    'n_step_per_snapshot': 50,
    'n_step_dynamics': 1000})
  run(parms)

  in_top, in_crds, in_vels = get_restart_files('md')

  util.goto_dir(os.path.join('..', md_dirs[1]))
  parms = constant_energy_parms.copy()
  parms['topology'] = top
  parms['input_crds'] = crd
  parms['output_name'] = 'md'
  parms['n_step_per_snapshot'] = 50
  parms['n_step_dynamics'] = 10000
  run(parms)
  in_top, in_crds, in_vels = get_restart_files('md')

  util.goto_dir(os.path.join('..', md_dirs[2]))
  parms = langevin_thermometer_parms.copy()
  parms['topology'] = top
  parms['input_crds'] = crd
  parms['output_name'] = 'md'
  parms['temp_thermometer'] = temperature
  parms['temp_initial'] = temperature
  parms['n_step_per_snapshot'] = 50
  parms['n_step_dynamics'] = 1000
  run(parms)
  
  util.goto_dir('..')
  merge_simulations(out_name, md_dirs)


# ##########################################################

# # 4. Read trajectories with some post-processing

# The units used in these files are:
# - positions: angstroms
# - velocities: angs/ps/20.455 


class TrjReader:
  """
  A class to read AMBER .trj files.

  Since the .trj files do not tell us how many atoms are in each frame, this
  must be read from the corresponding .top file. Hence initialization always
  requires a .top file as well. 

  Can also handle .trj.gz files.

  As well, the periodic box coordinates can also be read from the .trj files.

  It is initialized by:
    trj = TrjReader('md.top', 'md.trj')
    trj.top
    trj.trj
    trr.topology
    trr.n_frame

  The main function is:
    trr.load_frame(i)

  Which affects
    trr.i_frame
    trr.frame = [] list of 3*n_atom floats

  This can be directly accessed by:
    frame = trr[i]

  """
  
  def __init__(self, top, trj):
    self.top = top
    self.trj = trj

    self.topology = read_top(top)

    if '.vel.trj' in trj:
      # box information is not stored in velocitiy .trj files
      self.is_box_dims = False
    else:
      self.is_box_dims = self.topology['IFBOX'] > 0

    # Since .trj is a text format, it can be readily gzip'd,
    # so opening .trj.gz is a useful option to have.
    if self.trj.split(".")[-1].strip().lower() == "gz":
      self._file = gzip.GzipFile(self.trj, "r")
    else:
      self._file = open(self.trj, "r")

    # only 1-line header, frames starts after this line
    self.pos_start_frame = len(self.file.readline())

    self.n_atom = self.topology['NATOM']
    if self.n_atom == 0:
      raise Exception("No atoms found in .top file")

    # calculate the size of each frame
    n_line = (3 * self.n_atom) / 10
    if (3 * self.n_atom) % 10 > 0: 
      n_line += 1
    if self.is_box_dims:
      n_line += 1
    self.size_frame = 0
    for i in range(0, n_line):
      self.size_frame += len(self.file.readline())
      
    # calculate n_frame from end of file
    self.file.seek(0, 2)
    pos_eof = self.file.tell()
    self.n_frame = int((pos_eof - self.pos_start_frame) / self.size_frame)

    self.load_frame(0)

  def load_frame(self, i):
    """Loads the frame into self.frame, a list of 3*n_atom floats"""
    # Check bounds
    if i < - 1*self.n_frame or i >= self.n_frame:
      raise IndexError
    elif i < 0:
      i = self.n_frame + i

    self.file.seek(self.pos_start_frame + i*(self.size_frame))

    # read frame as list of 3 floats
    s = self.file.read(self.size_frame).rstrip()
    vals = [float(s[i:i+8]) for i in xrange(0, len(s), 8)]
    if self.is_box_dims:
      # drop the box dimension values
      vals = vals[:-3]
    if len(vals) != 3*self.n_atom:
      raise ValueError("Improper number of coordinates in frame.")

    self.frame = vals
    self.i_frame = i

  def __getitem__(self, i):
    """Gets the list of 3xn_atom floats for frame i"""
    self.load_frame(i)
    return self.frame

  def save_to_crd(self, crd):
    """
    Saves coordinates of current frame to an AMBER .crd file.
    """
    f = open(crd, "w")

    coords = self.frame

    f.write("ACE".ljust(80) + "\n")
    f.write("%5d  0.0000000E+00\n" % (len(coords) // 3))

    p = ["%12.7f" % x for x in coords]

    n_val_per_line = 6
    r = len(p) % n_val_per_line
    if r > 0: 
      p.extend([""] * (n_val_per_line - r))

    for i in xrange(0, len(p), n_val_per_line):
      f.write("".join(p[i:i + n_val_per_line]) + "\n")

    f.close()  

  def __repr__(self):
    return "< Amber Coord file %s with %d frames of %d atoms >" % \
             (self.trj, self.n_frame, self.n_atom)
    

class Trajectory:
  """
  Class to interact with an AMBER trajctory using soup.
  
  Class to interaction with a GROMACS trajctory.

  It is initialized by:
    traj = Trajectory('md')
    traj.basename
    traj.top
    traj.trj
    traj.vel_trj
    traj.n_frame
    traj.trr_reader

  Main method is:
    traj.load_frame(35)

  Which modifies:
    traj.i_frame
    traj.soup - a PDBREMIX pdbatoms.Polymer object
  """
  def __init__(self, basename):
    self.basename = basename
    self.top = basename + '.top'
    
    self.trj = basename + '.trj'
    self.trj_reader = TrjReader(self.top, self.trj)

    self.vel_trj = basename + '.vel.trj'
    if os.path.isfile(self.vel_trj):
      self.vel_trj_reader = TrjReader(self.top, self.vel_trj)
    else:
      self.vel_trj_reader = None

    # Create a soup object from self.top and the first frame of trj_reader
    self.soup = soup_from_topology(self.trj_reader.topology)
    self.trj_reader.save_to_crd(self.basename+'temp.crd')
    load_crd_or_rst_into_soup(self.soup, self.basename+'temp.crd')
    util.clean_fname(self.basename+'temp.crd')

    self.n_frame = len(self.trj_reader)
    self.load_frame(0)

  def load_frame(self, i):
    # Load coordinates of soup with coordinates from self.trj_reader
    coords = self.trj_reader[i]
    for j, a in enumerate(self.soup.atoms()):
      k = 3*j
      v3.set_vector(a.pos, coords[k], coords[k+1], coords[k+2])
    self.i_frame = self.trj_reader.i_frame

    # Load velocities of soup with coordinates from self.vel_trj_reader
    if self.vel_trj_reader is not None:
      vels = self.vel_trj_reader[i]
      for j, a in enumerate(self.soup.atoms()):
        k = 3*j
        v3.set_vector(a.vel, vels[k], vels[k+1], vels[k+2])


def merge_trajectories(top, trajs, out_traj):
  """
  Given a list of traj filenames (trajs), merges them into one complete
  trajectory (out_traj) using top to work out the number of atoms, and
  hence the size of the frame of the trajectory.
  """
  # Get pos_start_frame and size_frame by opening one of the 
  # trajectories via trj_reader
  trj_reader = TrjReader(top, trajs[0])
  pos_start_frame = trj_reader.pos_start_frame  
  size_frame = trj_reader.size_frame
  del trj_reader

  # Start the merged file by copying the first piece
  shutil.copy(trajs[0], out_traj)

  # Now open the merged file in appended form and add it to the
  # merged file, one frame at a time
  merge_traj_file = open(out_traj, "ab+")
  for traj in trajs[1:]:
    traj_file = open(traj, "rb")
    traj_file.seek(-1, 2)
    eof = traj_file.tell()
    traj_file.seek(pos_start_frame)
    while traj_file.tell() < eof:
      merge_traj_file.write(traj_file.read(size_frame)) 
    traj_file.close()
  merge_traj_file.close()


def merge_simulations(basename, pulses):
  """
  Given a list of directories with partial trajectories in each directory
  with the same basename for the md, will splice them together into one uber
  simulation.
  """
  shutil.copy(os.path.join(pulses[0], basename + '.sander.in'), basename + '.sander.in')
  shutil.copy(os.path.join(pulses[0], basename + '.top'), basename + '.top')
  shutil.copy(os.path.join(pulses[-1], basename + '.rst'), basename + '.rst')

  # merge energies of pulses into one energy file
  f = open(basename + '.energy', 'w')
  f.write('[\n')
  n_step = 0
  time = 0.0
  for pulse in pulses:
    energy_fname = os.path.join(pulse, basename + '.energy')
    if os.path.isfile(energy_fname):
      blocks = eval(open(energy_fname).read())
    else:
      sander_out = os.path.join(pulse, basename + '.sander.out')
      blocks = read_time_blocks(sander_out)
      for block in blocks:
        block_n_step = int(block['NSTEP'])
        block_time = float(block['TIME(PS)'])
        block['NSTEP'] = str(block_n_step + n_step)
        block['TIME(PS)'] = str(block_time + time)
        f.write(str(block) + ',\n')
    n_step = int(blocks[-1]['NSTEP'])
    time = float(blocks[-1]['TIME(PS)'])
  f.write(']\n')
  f.close()
    
  trajs = [os.path.join(pulse, basename + '.trj') for pulse in pulses]
  merge_trajectories(basename + '.top', trajs, basename + '.trj')

  vels = [os.path.join(pulse, basename + '.vel.trj') for pulse in pulses]
  merge_trajectories(basename + '.top', vels, basename + '.vel.trj')
  

def convert_crd_to_trj_frame(crd):
  """
  Returns a string that corresponds to a frame in a .trj from a
  .crd file. This is for writing to .trj files.
  """
  vals = [float(word) for word in util.words_in_file(crd)[1:]]
  lines = []
  line = ''
  for i in range(0, len(vals)):
    line += "%8.3f" % vals[i]
    if (i % 10) == 9:
      lines.append(line)
      line = ''
  if line:
    lines.append(line)
  return '\n'.join(lines) + '\n'


def read_dynamics_sander_out(sander_out):
  """
  Returns a list of dictionaries containing energy values
  from sander out file for molecular dynamics.
  """
  results = []
  block_dict = {}
  is_header = True
  for line in open(sander_out):
    if is_header:
      if '4' in line and 'RESULTS' in line:
        is_header = False
      continue
    if 'A V E R A G E S' in line:
      # End of time blocks
      break
    if line.startswith('|'):
      continue
    if '----' in line:
      # New block: save last block
      if block_dict:
        results.append(block_dict.copy())
    else:
      words = line.split()
      for i in range(len(words)):
        if words[i] == "=":
          key = words[i-1].strip()
          val = words[i+1]
          if key == 'NSTEP':
            block_dict[key] = int(val)
          else:
            block_dict[key] = float(val)
  return results


def read_minimization_sander_out(sander_out):
  """
  Returns a list of dictionaries containing energy values
  from sander out file for minimization steps.
  """
  results = []
  block_dict = {}
  lines = open(sander_out).readlines()
  is_results = False
  for i, line in enumerate(lines):
    if not is_results:
      if '4' in line and 'RESULTS' in line:
        is_results = True
        continue
    if 'NSTEP' in line and 'ENERGY' in line:
      if block_dict:
        results.append(block_dict.copy())
      words = lines[i+1].split()
      block_dict['NSTEP'] = int(words[0])
      block_dict['ENERGY'] = float(words[1])
      for line in lines[i+3:i+6]:
        pieces = line[:25], line[25:50], line[50:]
        for piece in pieces:
          key, value = piece.split('=')
          block_dict[key.strip()] = float(value)
  return results


def calculate_energy(top, crd):
  """
  Returns potential energy of top and crd by running sander
  and parsing the sander output.
  """
  top = os.path.abspath(top)
  crd = os.path.abspath(crd)

  util.goto_dir('energy-temp')

  parms = minimization_parms.copy()
  parms.extend({
    'topology': top,
    'input_crds': crd,
    'output_name': 'energy',
    'n_step_minimization': 0,
    'n_step_steepest_descent': 0})
  run(parms)

  blocks = read_minimization_sander_out('energy.sander.out')

  util.goto_dir('..')
  util.clean_fname('energy-temp')

  return blocks[0]['ENERGY']





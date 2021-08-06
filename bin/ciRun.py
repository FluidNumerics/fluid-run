#!/usr/bin/python3

import errno
import argparse
import json
import os
import subprocess
import shutil
import shlex
from datetime import datetime
import sys
import time
import yaml


DEFAULT_RETURN_CODE=999
SLEEP_INTERVAL=5
SLURMGCP_CONFIG = "/slurm/scripts/config.yaml"

def gceClusterRun(settings,tests):
    """Executes all execution_commands sequentially on GCE Cluster"""

    WORKSPACE='/workspace/'
    utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    k=0
    for test in tests['tests'] :

        workdir=WORKSPACE+test['output_directory']
        print('Making directory {}\n'.format(workdir),flush=True)
        try:
            os.makedirs(workdir)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            pass

        os.chdir(workdir)

        if settings['artifact_type'] == 'singularity':
            if settings['mpi'] :
  
                cmd = 'mpirun -np {NPROC} {AFFINITY} singularity exec --bind /workspace:/workspace {IMAGE} {CMD}'.format(NPROC=settings['nproc'],
                        AFFINITY=settings['task_affinity'],
                        IMAGE=WORKSPACE+settings['singularity_image'],

                        CMD=test['execution_command'])

            else:
  
                if int(settings['gpu_count']) > 0:
                    cmd = 'singularity exec --nv --bind /workspace:/workspace {IMAGE} {CMD}'.format(IMAGE=WORKSPACE+settings['singularity_image'],CMD=test['execution_command'])
                else:
                    cmd = 'singularity exec --bind /workspace:/workspace {IMAGE} {CMD}'.format(IMAGE=WORKSPACE+settings['singularity_image'],CMD=test['execution_command'])

       
        else:
            cmd = test['execution_command']


        print('Running {}\n'.format(cmd),flush=True)
        proc = subprocess.Popen(cmd,
                                shell=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        print(stdout.decode("utf-8"),flush=True)
        print(stderr.decode("utf-8"),flush=True)
        tests['tests'][k]['stdout'] = stdout.decode("utf-8")
        tests['tests'][k]['stderr'] = stderr.decode("utf-8")
        tests['tests'][k]['exit_code'] = proc.returncode
        tests['tests'][k]['build_id'] = settings['build_id']
        tests['tests'][k]['machine_type'] = settings['machine_type']
        tests['tests'][k]['node_count'] =int(settings['node_count'])
        tests['tests'][k]['gpu_type'] = settings['gpu_type']
        tests['tests'][k]['gpu_count'] =int(settings['gpu_count'])
        tests['tests'][k]['git_sha'] = settings['git_sha']
        tests['tests'][k]['datetime'] = utc

        k+=1
                                        
    # Change working directory back to /workspace
    os.chdir(WORKSPACE)
    with open(WORKSPACE+'/results.json','w')as f:          
        f.write(json.dumps(tests))

#END gceClusterRun

def run(cmd):
    """Runs a command in the local environment and returns exit code, stdout, and stderr"""

    proc = subprocess.Popen(cmd,
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)

    stdout, stderr = proc.communicate()

    return stdout, stderr, proc.returncode

#END run

def slurmgcpRun(settings,tests):
    """Executes command_groups sequentially on a Slurm-GPC cluster"""

    WORKSPACE='/apps/workspace/'
    sbatch = '/usr/local/bin/sbatch '
    squeue = '/usr/local/bin/squeue '
    sacct = '/usr/local/bin/sacct '

    utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
   
    command_groups = {}
    k=0
    for test in tests['tests'] :

        workdir=WORKSPACE+test['output_directory']
        print('Making directory {}\n'.format(workdir),flush=True)
        try:
            os.makedirs(workdir)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            pass

        os.chdir(workdir)

        cmd = sbatch

        # Create job dependency for job submissions
        if test['command_group'] in command_groups.keys():
            dependent_job = command_groups[test['command_group']][-1]['job_id']
            cmd += '--dependency=afterany:{} '.format(str(dependent_job))


        # Add partition flag to job submission
        if 'partition' in test.keys():
            print('Submitting {} to partition {}'.format(test['execution_command'],test['partition']),flush=True)
            cmd += '--partition={} '.format(test['partition'])
        else:
            print('Submitting {} to default partition'.format(test['execution_command']),flush=True)


        # Add stdout/stderr for later parsing
        cmd += '-o {}/{}/stdout '.format(WORKSPACE,test['output_directory'])
        cmd += '-e {}/{}/stderr '.format(WORKSPACE,test['output_directory'])

        # Add batch file to command
        cmd += WORKSPACE+test['execution_command']

        print(cmd,flush=True)

        # Submit the job
        stdout, stderr, returncode = run(cmd)

        # Log information
        tests['tests'][k]['stdout'] = ''
        tests['tests'][k]['stderr'] = ''
        tests['tests'][k]['exit_code'] = DEFAULT_RETURN_CODE
        tests['tests'][k]['build_id'] = settings['build_id']
        tests['tests'][k]['git_sha'] = settings['git_sha']
        tests['tests'][k]['datetime'] = utc
        tests['tests'][k]['node_count'] =int(settings['node_count'])
        tests['tests'][k]['machine_type'] = settings['machine_type']
        tests['tests'][k]['gpu_type'] = settings['gpu_type']
        tests['tests'][k]['gpu_count'] =int(settings['gpu_count'])

        # Check return code
        if returncode == 0:
            # Get the job id
            jobid = int(stdout.decode('utf-8').split(' ')[-1])
            # Record the job id in the command_group
            if test['command_group'] in command_groups.keys():
                command_groups[test['command_group']].append({'job_id':jobid,'index':k,'complete':False})
            else:
                command_groups[test['command_group']] = [{'job_id':jobid,'index':k,'complete':False}]
        else:
            print(stderr.decode('utf-8'),flush=True)
            tests['tests'][k]['stdout'] = 'sbatch stdout : '+stdout.decode("utf-8")
            tests['tests'][k]['stderr'] = 'sbatch stderr : '+stderr.decode("utf-8")
            tests['tests'][k]['exit_code'] = returncode

        k+=1
        time.sleep(0.5)


    njobs = k
    ncomplete = 0
    # Monitor Jobs
    while True:

        if ncomplete == njobs:
            break

        print('Jobs status : {}/{}'.format(str(ncomplete),str(njobs),flush=True))
        for cg in command_groups.keys():
            k=0
            for test in command_groups[cg]:
                if not test['complete']:
                    jobid = test['job_id']
                    index = test['index']
                    cmd = 'sacct -j {} --format=state%10'.format(str(jobid))
                    stdout, stderr, returncode = run(cmd)
                    status = stdout.decode('utf-8').split('\n')[-2].strip()
                    print('Job {} status : {}'.format(str(jobid),status))
                    if status == 'COMPLETED' or status == 'FAILED':
                        ncomplete += 1
                        command_groups[cg][k]['complete'] = True

                        # Log stdout and stderr
                        with open('{}/{}/stdout'.format(WORKSPACE,tests['tests'][index]['output_directory']),'r') as f:
                            stdout = f.read()

                        with open('{}/{}/stderr'.format(WORKSPACE,tests['tests'][index]['output_directory']),'r') as f:
                            stderr = f.read()

                        tests['tests'][index]['stdout'] = stdout
                        tests['tests'][index]['stderr'] = stderr

                        # Get return code from sacct
                        cmd = 'sacct -j {} --format=exitCode%10'.format(str(jobid))
                        stdout, stderr, returncode = run(cmd)
                        returncode = stdout.decode('utf-8').split('\n')[-2].strip().split(':')[0]
                        tests['tests'][index]['exit_code'] = int(returncode)

                        # Get the number of nodes
                        cmd = 'sacct -j {} --format=NNodes'.format(str(jobid))
                        stdout, stderr, returncode = run(cmd)
                        nnodes = stdout.decode('utf-8').split('\n')[-2].strip()
                        tests['tests'][index]['node_count'] = int(nnodes)

                        # Get the node list and record the machine information
                        cmd = 'sacct -j {} --format=nodelist%30'.format(str(jobid))
                        stdout, stderr, returncode = run(cmd)
                        instance = '-'.join(stdout.decode('utf-8').split('\n')[-2].strip().split('-')[:-2])
                        with open(SLURMGCP_CONFIG, 'r') as stream:
                            try:
                                slurmConfig = yaml.safe_load(stream)
                            except yaml.YAMLError as exc:
                                print(exc,flush=True)

                        if instance in slurmConfig['instance_defs'].keys():
                            tests['tests'][index]['machine_type'] = slurmConfig['instance_defs'][instance]['machine_type']
                            tests['tests'][index]['gpu_type'] = slurmConfig['instance_defs'][instance]['gpu_type']
                            tests['tests'][index]['gpu_count'] =int(slurmConfig['instance_defs'][instance]['gpu_count'])

                k+=1

        time.sleep(SLEEP_INTERVAL)

        
    # Change working directory back to /workspace
    os.chdir(WORKSPACE)
    with open(WORKSPACE+'/results.json','w')as f:          
        f.write(json.dumps(tests))

#END slurmgcpRun

def main():

    if os.path.isdir('/workspace'):
        print('Found settings in /workspace',flush=True)
        WORKSPACE = '/workspace/'
    elif os.path.isdir('/apps/workspace'):
        print('Found settings in /apps/workspace',flush=True)
        WORKSPACE = '/apps/workspace/'
    else:
        print('Workspace on cluster not found. Quitting',flush=True)
        sys.exit(1)

    with open(WORKSPACE+'settings.json','r')as f: 
        settings = json.load(f)

    with open(WORKSPACE+settings['ci_file'],'r')as f: 
        tests = json.load(f)

    if settings['slurm_controller']:
        slurmgcpRun(settings,tests)
    else:
        gceClusterRun(settings,tests)


#END main

if __name__=='__main__':
    main()

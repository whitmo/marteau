import random
import os
import subprocess
import sys
import fcntl
import argparse
import logging
import tempfile


from marteau import __version__, logger
from marteau.config import read_config
from marteau.redirector import Redirector
from marteau import queue


workdir = '/tmp'
reportsdir = '/tmp'


LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG}

LOG_FMT = r"%(asctime)s [%(process)d] [%(levelname)s] %(message)s"
LOG_DATE_FMT = r"%Y-%m-%d %H:%M:%S"


def _stream(data):
    sys.stdout.write(data['data'])
    # XXX we'll push this in memory somewhere for
    # the web app to display it live
    job_id = queue.pid_to_jobid(os.getpid())
    if job_id is not None:
        queue.append_console(job_id, data['data'])


def run_func(cmd, stop_on_failure=True):
    redirector = Redirector(_stream)
    redirector.start()
    logger.debug(cmd)
    try:
        process = subprocess.Popen(cmd, shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        redirector.add_redirection('marteau', process, process.stdout)
        redirector.add_redirection('marteau', process, process.stderr)
        process.wait()
        res = process.returncode
        if res != 0 and stop_on_failure:
            raise Exception("%r failed" % cmd)
        return res
    finally:
        redirector.kill()


run_bench = "%s -c 'from funkload.BenchRunner import main; main()'"
run_bench = run_bench % sys.executable
run_report = "%s -c 'from funkload.ReportBuilder import main; main()'"
run_report = run_report % sys.executable
run_pip = "%s -c 'from pip import runner; runner.run()'"
run_pip = run_pip % sys.executable


def run_loadtest(repo, cycles=None, nodes_count=None, duration=None):
    if os.path.exists(repo):
        # just a local dir, lets work there
        os.chdir(repo)
    else:
        # checking out the repo
        os.chdir(workdir)
        name = repo.split('/')[-1].split('.')[0]
        target = os.path.join(workdir, name)
        if os.path.exists(target):
            os.chdir(target)
            run_func('git pull')
        else:
            run_func('git clone %s' % repo, stop_on_failure=False)
            os.chdir(target)

    # now looking for the marteau config file in there
    config = read_config(os.getcwd())

    wdir = config.get('wdir')
    if wdir is not None:
        os.chdir(wdir)

    # creating a virtualenv there
    run_func('virtualenv --no-site-packages .')
    run_func(run_pip + ' install funkload')

    # install dependencies if any
    deps = config.get('deps', [])
    for dep in deps:
        run_func(run_pip + ' install %s' % dep)

    # is this a distributed test ?
    if nodes_count is None:
        nodes_count = config.get('nodes', 1)

    distributed = nodes_count > 1

    if distributed:
        # we want to pick up the number of nodes asked
        nodes = [node for node in queue.get_nodes()
                 if node.status == 'idle']

        if len(nodes) < nodes_count:
            # XXX we want to pile this one back !
            raise ValueError("Sorry could not find enough free nodes")

        # then pick random ones
        random.shuffle(nodes)
        nodes = nodes[:nodes_count]

        # save the nodes status
        for node in nodes:
            node.status = 'working'
            queue.save_node(node)

        workers = ','.join([node.name for node in nodes])
        os.environ['MARTEAU_NODES'] = workers

        workers = '--distribute-workers=%s' % workers
        cmd = '%s --distribute %s' % (run_bench, workers)
        if deps != []:
            cmd += ' --distributed-packages=%s' % ' '.join(deps)

        target = tempfile.mkdtemp()
        cmd += ' --distributed-log-path=%s' % target
        target = os.path.join(target, '*.xml')
    else:
        cmd = run_bench
        target = config['xml']

    if cycles is not None:
        cmd += ' --cycles=%s' % cycles

    if duration is not None:
        cmd += ' --duration=%s' % duration

    report_dir = os.path.join(reportsdir,
            os.environ.get('MARTEAU_JOBID', 'report'))

    run_func('%s %s %s' % (cmd, config['script'], config['test']))
    run_func(run_report + ' --html -r %s  %s' % (report_dir, target))
    return report_dir


def close_on_exec(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    flags |= fcntl.FD_CLOEXEC
    fcntl.fcntl(fd, fcntl.F_SETFD, flags)


def configure_logger(logger, level='INFO', output="-"):
    loglevel = LOG_LEVELS.get(level.lower(), logging.INFO)
    logger.setLevel(loglevel)
    if output == "-":
        h = logging.StreamHandler()
    else:
        h = logging.FileHandler(output)
        close_on_exec(h.stream.fileno())
    fmt = logging.Formatter(LOG_FMT, LOG_DATE_FMT)
    h.setFormatter(fmt)
    logger.addHandler(h)


def main():
    parser = argparse.ArgumentParser(description='Drives Funkload.')
    parser.add_argument('repo', help='Git repository or local directory',
                        nargs='?')
    parser.add_argument('--version', action='store_true',
                     default=False, help='Displays Circus version and exits.')
    parser.add_argument('--log-level', dest='loglevel', default='info',
            choices=LOG_LEVELS.keys() + [key.upper() for key in
                LOG_LEVELS.keys()],
            help="log level")
    parser.add_argument('--log-output', dest='logoutput', default='-',
            help="log output")

    args = parser.parse_args()

    if args.version:
        print(__version__)
        sys.exit(0)

    if args.repo is None:
        parser.print_usage()
        sys.exit(0)

    # configure the logger
    configure_logger(logger, args.loglevel, args.logoutput)

    logger.info('Hammer ready. Where are the nails ?')
    try:
        return run_loadtest(args.repo)
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        logger.info('Bye!')


if __name__ == '__main__':
    main()

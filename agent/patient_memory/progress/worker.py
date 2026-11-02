import argparse,time
from backend.db import SessionLocal
from .runtime import schedule,process_once


def main():
    parser=argparse.ArgumentParser(description='Persistent patient-matter progress worker')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--poll-interval',type=float,default=5)
    args=parser.parse_args()
    if args.poll_interval<1:parser.error('poll interval must be at least 1 second')
    try:
        while True:
            try:
                with SessionLocal() as db:
                    schedule(db);result=process_once(db)
                    if result:print(result,flush=True)
            except Exception as exc:
                if args.once:raise
                print({'status':'MATTER_WORKER_ERROR','error_type':type(exc).__name__},flush=True)
            if args.once:break
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:pass


if __name__=='__main__':main()

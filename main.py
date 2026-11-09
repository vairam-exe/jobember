
from src.client.naukri_client import NaukriLoginClient
from src.client.job_client import NaukriJobClient
from dotenv import load_dotenv
from colorama import Fore, Style, init
import os
load_dotenv()
import time
from datetime import datetime

init(autoreset=True)

if __name__ == "__main__":
    # ---------------------------------------------------------------
    # Load credentials from .env file
    # (NAUKRI_USERNAME and NAUKRI_PASSWORD must be set)
    # ---------------------------------------------------------------
    username = os.getenv("USERNAME")
    password = os.getenv("PASSWORD")

    # ---------------------------------------------------------------
    # 1. Login — authenticates and stores session + bearer token
    # ---------------------------------------------------------------
    client = NaukriLoginClient(username, password)
    client.login()
    # # ---------------------------------------------------------------
    # # 2. Resume upload — uploads a new PDF resume to your profile,provide the file path 
    # # ---------------------------------------------------------------
    # print(client.update_resume(r"C:/Users/HP/Downloads/my_resume2.pdf"))

    # # ---------------------------------------------------------------
    # # 3. Profile update — update headline and summary independently
    # #    Both fields are optional, pass only what you want to change
    # # ---------------------------------------------------------------
    # print(client.update_profile(headline="Software Engineer with 2.3 years of experience in backend development using Node.js, Python, AWS, SQL, and NoSQL."
    # ))

    # print(client.update_profile(summary="this is my summary"))

    # # ---------------------------------------------------------------
    # # 4. Misc — fetch profile ID and form key (mostly for debugging)
    # # ---------------------------------------------------------------
    # # print(client.fetch_profile_id())
    # # print(client.get_form_key2())

    # # ---------------------------------------------------------------
    # # 5. Recommended jobs — fetches personalised job listings
    # #    based on your Naukri profile
    # # ---------------------------------------------------------------
    jc = NaukriJobClient(client)
    # jobs = jc.get_recommended_jobs()

    # print("Fetching recommended jobs...")
       



    
    print("Searching jobs...")    
    jobs = jc.search_jobs(keyword="software engineer", location="Chennai", experience=2)

    if not jobs:
        print(f"{Fore.YELLOW}  No jobs found.{Style.RESET_ALL}")
    else:
        print(f"{Fore.GREEN}Found {len(jobs)} jobs{Style.RESET_ALL}")

        for job in jobs:
            print(f"\n{Fore.CYAN}{'─'*50}{Style.RESET_ALL}")
            print(f"{Fore.WHITE}  Title   : {Fore.YELLOW}{job.title}")
            print(f"{Fore.WHITE}  Company : {Fore.YELLOW}{job.company}")
            print(f"{Fore.WHITE}  Job ID  : {Fore.YELLOW}{job.job_id}")
            print(f"{Fore.WHITE}  Tags    : {Fore.YELLOW}{job.tags}")

            mandatory = job.tags[:2] if job.tags else []
            optional  = job.tags[2:] if len(job.tags) > 2 else []

            try:
                result = jc.apply_job(job, mandatory_skills=mandatory, optional_skills=optional, source="recommended")

                # Check questionnaire
                job_result = (result.get("jobs") or [{}])[0]
                if job_result.get("questionnaire"):
                    print(f"{Fore.CYAN}   📋 Questionnaire required — generating answers...{Style.RESET_ALL}")
                    sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"
                    
                    candidate_profile = {
                        "current_ctc":       "500000",
                        "current_ctc_lakhs": "5",
                        "expected_ctc":      "1000000",
                        "expected_ctc_lakhs": "10",
                        "exp_total":         "2",
                        "exp_node":          "2",
                        "exp_python":        "2",
                        "exp_java":          "2",
                        "notice_days":       30,
                        "location":          "Chennai",
                        "dob":               "11/08/2002",
                        "ex_emp_id":         "NA",
                    }

                    q_result = jc.handle_static_questionnaire_and_apply(
                        job,
                        questionnaire=job_result["questionnaire"],
                        sid=sid,
                        mandatory_skills=mandatory,
                        optional_skills=optional,
                        source="search",
                        profile=candidate_profile,
                    )

                    unanswered = q_result.get("unanswerable") or []
                    if unanswered:
                        for uq in unanswered:
                            print(f"{Fore.MAGENTA}{Style.BRIGHT}   ⚠️  [UNANSWERABLE QUESTION] Q: \"{uq['question']}\" ({uq['type']}){Style.RESET_ALL}")
                            print(f"{Fore.YELLOW}      Options: {uq['options']} -> Fallback applied: {uq['fallback']}{Style.RESET_ALL}")

                    if q_result.get("success") is False:
                        print(f"{Fore.RED}   Failed to apply questionnaire: {q_result.get('error')}{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.GREEN}  ✅ Applied successfully (with questionnaire answers)!{Style.RESET_ALL}")
                    time.sleep(3)
                    continue

                print(f"{Fore.GREEN}  ✅ Applied successfully!{Style.RESET_ALL}")

            except Exception as e:
                print(f"{Fore.RED}   Failed: {e}{Style.RESET_ALL}")
            
            time.sleep(3)






    # --------------------------------------------------------------- 
    # 6. scrap the jobs,example
    #     
    # ---------------------------------------------------------------
    # i = 1
    # while True:
    #     job_list = jc.search_jobs("Node.js", location="Hyderabad", experience=1, page=i)
    #     for count, job in enumerate(job_list):
    #         print(count + 1, ":-", job.title, " :- ", job.company)
        
    #     breaker = input("enter anything for next page, q to quit: ")
    #     if breaker.strip().lower() == "q":
    #         break
    #     i += 1
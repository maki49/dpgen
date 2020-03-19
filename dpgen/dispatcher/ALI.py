from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.acs_exception.exceptions import ClientException
from aliyunsdkcore.acs_exception.exceptions import ServerException
from aliyunsdkecs.request.v20140526.RunInstancesRequest import RunInstancesRequest
from aliyunsdkecs.request.v20140526.DeleteInstancesRequest import DeleteInstancesRequest
from aliyunsdkecs.request.v20140526.DescribeAutoProvisioningGroupInstancesRequest import DescribeAutoProvisioningGroupInstancesRequest
from aliyunsdkecs.request.v20140526.CreateAutoProvisioningGroupRequest import CreateAutoProvisioningGroupRequest
from aliyunsdkecs.request.v20140526.DeleteAutoProvisioningGroupRequest import DeleteAutoProvisioningGroupRequest
from aliyunsdkecs.request.v20140526.ModifyAutoProvisioningGroupRequest import ModifyAutoProvisioningGroupRequest
from aliyunsdkecs.request.v20140526.DeleteLaunchTemplateRequest import DeleteLaunchTemplateRequest
from aliyunsdkvpc.request.v20160428.DescribeVpcsRequest import DescribeVpcsRequest
from aliyunsdkecs.request.v20140526.DescribeLaunchTemplatesRequest import DescribeLaunchTemplatesRequest
from aliyunsdkecs.request.v20140526.CreateLaunchTemplateRequest import CreateLaunchTemplateRequest
from aliyunsdkecs.request.v20140526.DescribeImagesRequest import DescribeImagesRequest
from aliyunsdkecs.request.v20140526.DescribeSecurityGroupsRequest import DescribeSecurityGroupsRequest
import time, json, os, glob, string, random
from dpgen.dispatcher.Dispatcher import Dispatcher, _split_tasks, JobRecord
from dpgen.dispatcher.SSHContext import SSHSession
from os.path import join
from dpgen  import dlog
from hashlib import sha1

def manual_delete(stage):
    with open('machine-ali.json') as fp1:
        mdata = json.load(fp1)
        adata = mdata[stage][0]['machine']['ali_auth']
        mdata_resources = mdata[stage][0]['resources']
        mdata_machine = mdata[stage][0]['machine']
        ali = ALI(adata, mdata_resources, mdata_machine, 0)
        with open('machine_record.json', 'r') as fp2:
            machine_record = json.load(fp2)
            ali.instance_list = machine_record['instance_id']
            ali.delete_machine()

def manual_create(stage, machine_number):
    with open('machine-ali.json') as fp:
        mdata = json.load(fp)
        adata = mdata[stage][0]['machine']['ali_auth']
        mdata_resources = mdata[stage][0]['resources']
        mdata_machine = mdata[stage][0]['machine']
        ali = ALI(adata, mdata_resources, mdata_machine, machine_number)
        ali.alloc_machine()
        print(ali.ip_list)

class ALI():
    def __init__(self, adata, mdata_resources, mdata_machine, nchunks):
        self.ip_list = []
        self.instance_list = []
        self.dispatchers = []
        self.job_handlers = [None for i in range(nchunks)]
        self.task_chunks = None
        self.adata = adata
        self.apg_id = None
        self.template_id = None
        self.vsw_id = None
        self.regionID = adata["regionID"]
        self.client = AcsClient(adata["AccessKey_ID"], adata["AccessKey_Secret"], self.regionID)
        self.mdata_resources = mdata_resources
        self.mdata_machine = mdata_machine
        self.nchunks = nchunks

    def init(self, work_path, tasks, group_size):
        if self.check_restart(work_path, tasks, group_size):
            pass
        else:
            self.create_ess()
            self.dispatchers = self.make_dispatchers()

    def create_ess(self):
        img_id = self.get_image_id(self.adata["img_name"])
        sg_id, vpc_id = self.get_sg_vpc_id()
        self.template_id = self.create_template(img_id, sg_id, vpc_id)
        self.vsw_id = self.get_vsw_id(vpc_id)
        self.apg_id = self.create_apg()
        dlog.info("begin to create ess")
        time.sleep(90)
        self.instance_list = self.describe_apg_instances()
        self.ip_list = self.get_ip(self.instance_list)

    def delete_apg(self):
        request = DeleteAutoProvisioningGroupRequest()
        request.set_accept_format('json')
        request.set_AutoProvisioningGroupId(self.apg_id)
        request.set_TerminateInstances(True)
        response = self.client.do_action_with_exception(request)

    def create_apg(self):
        request = CreateAutoProvisioningGroupRequest()
        request.set_accept_format('json')
        request.set_TotalTargetCapacity(str(self.nchunks))
        request.set_LaunchTemplateId(self.template_id)
        request.set_AutoProvisioningGroupName(self.adata["instance_name"] + ''.join(random.choice(string.ascii_uppercase) for _ in range(20)))
        request.set_AutoProvisioningGroupType("maintain")
        request.set_SpotAllocationStrategy("lowest-price")
        request.set_SpotInstanceInterruptionBehavior("terminate")
        request.set_SpotInstancePoolsToUseCount(1)
        request.set_ExcessCapacityTerminationPolicy("termination")
        request.set_TerminateInstances(True)
        request.set_PayAsYouGoTargetCapacity("0")
        request.set_SpotTargetCapacity(str(self.nchunks))
        config = self.generate_config()
        request.set_LaunchTemplateConfigs(config)
        response = self.client.do_action_with_exception(request)
        response = json.loads(response)
        with open('apg_id.json', 'w') as fp:
            json.dump({'apg_id': response["AutoProvisioningGroupId"]}, fp, indent=4)
        return response["AutoProvisioningGroupId"]

    def update_instance_ip_list(self):
        instance_list = self.describe_apg_instances()
        ip_list = self.get_ip(instance_list)
        self.instance_list += list(set(instance_list) - set(self.instance_list))
        self.ip_list += list(set(ip_list) - set(self.ip_list))
        #dlog.info("%d,%d,%d,%d"%(len(instance_list), len(ip_list), len(self.instance_list), len(self.ip_list)))

    def describe_apg_instances(self):
        request = DescribeAutoProvisioningGroupInstancesRequest()
        request.set_accept_format('json')
        request.set_AutoProvisioningGroupId(self.apg_id)
        request.set_PageSize(100)
        iteration = self.nchunks // 100
        instance_list = []
        for i in range(iteration + 1):
            request.set_PageNumber(i+1)
            response = self.client.do_action_with_exception(request)
            response = json.loads(response)
            for ins in response["Instances"]["Instance"]:
                instance_list.append(ins["InstanceId"])
        #dlog.info(instance_list)
        return instance_list

    def generate_config(self):
        machine_config = self.adata["machine_type_price"]
        config = []
        for conf in machine_config:
            for vsw in self.vsw_id:
                tmp = {
                    "InstanceType": conf["machine_type"],
                    "MaxPrice": str(conf["price_limit"] * conf["numb"]),
                    "VSwitchId": vsw,
                    "WeightedCapacity": "1",
                    "Priority": str(conf["priority"])
                }
                config.append(tmp)
        return config

    def create_template(self, image_id, sg_id, vpc_id):
        request = CreateLaunchTemplateRequest()
        request.set_accept_format('json')
        request.set_LaunchTemplateName(''.join(random.choice(string.ascii_uppercase) for _ in range(20)))
        request.set_ImageId(image_id)
        request.set_ImageOwnerAlias("self")
        request.set_PasswordInherit(True)
        request.set_InstanceType("ecs.c6.large")
        request.set_SecurityGroupId(sg_id)
        request.set_VpcId(vpc_id)
        request.set_InternetMaxBandwidthIn(10)
        request.set_InternetMaxBandwidthOut(10)
        request.set_SystemDiskCategory("cloud_efficiency")
        request.set_SystemDiskSize(40)
        request.set_IoOptimized("optimized")
        request.set_InstanceChargeType("PostPaid")
        request.set_NetworkType("vpc")
        request.set_SpotStrategy("SpotWithPriceLimit")
        request.set_SpotPriceLimit(100)
        response = self.client.do_action_with_exception(request)
        response = json.loads(response)
        return response["LaunchTemplateId"]

    def delete_template(self):
        request = DeleteLaunchTemplateRequest()
        request.set_accept_format('json')
        request.set_LaunchTemplateId(self.template_id)
        response = self.client.do_action_with_exception(request)
        
    def get_image_id(self, img_name):
        request = DescribeImagesRequest()
        request.set_accept_format('json')
        request.set_ImageOwnerAlias("self")
        response = self.client.do_action_with_exception(request)
        response = json.loads(response)
        for img in response["Images"]["Image"]:
            if img["ImageName"] == img_name:
                return img["ImageId"]

    def get_sg_vpc_id(self):
        request = DescribeSecurityGroupsRequest()
        request.set_accept_format('json')
        response = self.client.do_action_with_exception(request)
        response = json.loads(response)
        for sg in response["SecurityGroups"]["SecurityGroup"]:
            if sg["SecurityGroupName"] == "sg":
                return sg["SecurityGroupId"], sg["VpcId"]

    def get_vsw_id(self, vpc_id):
        request = DescribeVpcsRequest()
        request.set_accept_format('json')
        request.set_VpcId(vpc_id)
        response = self.client.do_action_with_exception(request)
        response = json.loads(response)
        for vpc in response["Vpcs"]["Vpc"]:
            if vpc["VpcId"] == vpc_id:
                return vpc["VSwitchIds"]["VSwitchId"]

    def change_apg_capasity(self, capasity):
        request = ModifyAutoProvisioningGroupRequest()
        request.set_accept_format('json')
        request.set_AutoProvisioningGroupId(self.apg_id)
        request.set_TotalTargetCapacity(str(capasity))
        request.set_SpotTargetCapacity(str(capasity))
        request.set_PayAsYouGoTargetCapacity("0")
        response = self.client.do_action_with_exception(request)

    def spot_data_callback():
        pass

    def check_restart(self, work_path, tasks, group_size):
        if os.path.exists('apg_id.json'):
            with open('apg_id.json') as fp:
                apg = json.load(fp)
                self.apg_id = apg["apg_id"] 
            #dlog.info(self.apg_id)
            self.task_chunks = _split_tasks(tasks, group_size)
            task_chunks_str = ['+'.join(ii) for ii in self.task_chunks]
            task_hashes = [sha1(ii.encode('utf-8')).hexdigest() for ii in task_chunks_str]
            nchunks = len(self.task_chunks)
            for ii in range(nchunks):
                fn = 'jr.%.06d.json' % ii
                if not os.path.exists(os.path.join(os.path.abspath(work_path), fn)):
                    self.dispatchers.append([None, "unalloc"])
                else:
                    job_record = JobRecord(work_path, self.task_chunks, fname = fn)
                    cur_chunk = self.task_chunks[ii]
                    cur_hash = task_hashes[ii]
                    if not job_record.check_finished(cur_hash): 
                        with open(os.path.join(work_path, 'jr.%.06d.json' % ii)) as fp:
                            jr = json.load(fp)
                            ip = jr[cur_hash]['context'][3]
                            instance_id = jr[cur_hash]['context'][4]
                            self.ip_list.append(ip)
                            self.instance_list.append(instance_id)
                            profile = self.mdata_machine.copy()
                            profile['hostname'] = ip
                            profile['instance_id'] = instance_id
                            disp = Dispatcher(profile, context_type='ssh-uuid', batch_type='shell', job_record='jr.%.06d.json' % ii)
                            self.dispatchers.append([disp, "working"])
                    else:
                        with open(os.path.join(work_path, 'jr.%.06d.json' % ii)) as fp:
                            jr = json.load(fp)
                            ip = jr[cur_hash]['context'][3]
                            instance_id = jr[cur_hash]['context'][4]
                            self.ip_list.append(ip)
                            self.instance_list.append(instance_id)
                            self.dispatchers.append([None, "finished"])
            #dlog.info(self.ip_list)
            #dlog.info(self.dispatchers)
            return True
        else:
            self.task_chunks = _split_tasks(tasks, group_size)
            return False

    def get_ip(self, instance_list):
        request = DescribeInstancesRequest()
        request.set_accept_format('json')
        ip_list = []
        if len(instance_list) <= 10:
            for i in range(len(instance_list)):
                request.set_InstanceIds([instance_list[i]])
                response = self.client.do_action_with_exception(request)
                response = json.loads(response)
                ip_list.append(response["Instances"]["Instance"][0]["PublicIpAddress"]['IpAddress'][0])
        else:
            iteration = len(instance_list) // 10
            for i in range(iteration):
                for j in range(10):
                    request.set_InstanceIds([instance_list[i*10+j]])
                    response = self.client.do_action_with_exception(request)
                    response = json.loads(response)
                    ip_list.append(response["Instances"]["Instance"][0]["PublicIpAddress"]['IpAddress'][0])
            if len(instance_list) - iteration * 10 != 0:
                for j in range(len(instance_list) - iteration * 10):
                    request.set_InstanceIds([instance_list[iteration*10+j]])
                    response = self.client.do_action_with_exception(request)
                    response = json.loads(response)
                    ip_list.append(response["Instances"]["Instance"][0]["PublicIpAddress"]['IpAddress'][0])
        #dlog.info('create machine successfully, following are the ip addresses')
        # for ip in ip_list:
            # dlog.info(ip)
        return ip_list

    def get_finished_job_num(self):
        finished_num = 0
        for ii in range(len(self.dispatchers)):
            if self.dispatchers[ii][1] == "finished":
                finished_num += 1
        return finished_num

    def run_jobs(self,
                 resources,
                 command,
                 work_path,
                 tasks,
                 group_size,
                 forward_common_files,
                 forward_task_files,
                 backward_task_files,
                 forward_task_deference = True,
                 mark_failure = False,
                 outlog = 'log',
                 errlog = 'err'):
        for ii in range(self.nchunks):
            if self.dispatchers[ii][1] == "working":
                dlog.info(self.ip_list[ii])
                job_handler = self.dispatchers[ii][0].submit_jobs(resources,
                                                               command,
                                                               work_path,
                                                               self.task_chunks[ii],
                                                               group_size,
                                                               forward_common_files,
                                                               forward_task_files,
                                                               backward_task_files,
                                                               forward_task_deference,
                                                               outlog,
                                                               errlog)
                self.job_handlers[ii] = job_handler
        machine_exception_num = 0
        exception = []
        while True:
            #dlog.info(self.ip_list)
            #dlog.info(self.task_chunks)
            #dlog.info(exception_task_chunks)
            #dlog.info(exception_jr_name)
            if machine_exception_num / self.nchunks > 0.05:
                self.update_instance_ip_list()
                #dlog.info(self.ip_list)
                if len(self.ip_list) == len(self.task_chunks) + len(exception):
                    exception = sorted(exception, key=lambda x:x[2])
                    restart_ip_list = self.ip_list[-len(exception):]
                    restart_instance_list = self.instance_list[-len(exception):]
                    for ii in range(len(exception)):
                        #dlog.info(exception_task_chunks[ii])
                        #dlog.info(exception_jr_name[ii])
                        profile = self.mdata_machine.copy()
                        profile["hostname"] = restart_ip_list[ii]
                        profile["instance_id"] = restart_instance_list[ii]
                        disp = [Dispatcher(profile, context_type='ssh', batch_type='shell', job_record=exception[ii][1]), "working"]
                        pos = exception[ii][2]
                        self.dispatchers.insert(pos, disp)
                        self.ip_list.insert(pos, self.ip_list.pop(self.ip_list.index(profile["hostname"])))
                        self.instance_list.insert(pos, self.instance_list.pop(self.instance_list.index(profile["instance_id"])))
                        dlog.info(restart_ip_list[ii])
                        job_handler = disp[0].submit_jobs(resources,
                                                        command,
                                                        work_path,
                                                        exception[ii][0],
                                                        group_size,
                                                        forward_common_files,
                                                        forward_task_files,
                                                        backward_task_files,
                                                        forward_task_deference,
                                                        outlog,
                                                        errlog)
                        self.job_handlers.insert(pos, job_handler)
                        self.task_chunks.insert(pos, exception[ii][0])
                        exception.pop(ii)
            for ii in range(len(self.task_chunks)):
                if self.dispatchers[ii][1] == "working":
                    if self.check_server(self.dispatchers[ii][0]):
                        if self.dispatchers[ii][0].all_finished(self.job_handlers[ii], mark_failure):
                            self.dispatchers[ii][1] = "finished"
                            self.delete(ii)
                            self.change_apg_capasity(self.nchunks - self.get_finished_job_num())
                    else:
                        machine_exception_num += 1
                        exception.append([self.task_chunks.pop(ii), self.job_handlers[ii]["job_record"].fname[-14:], ii])
                        self.dispatchers.pop(ii)
                        self.ip_list.pop(ii)
                        self.instance_list.pop(ii)
                        self.job_handlers.pop(ii)
                        dlog.info("remove %s" % self.job_handlers[ii]["job_record"].fname[-14:])
                        os.remove(self.job_handlers[ii]["job_record"].fname)
                        break
                elif self.dispatchers[ii][1] == "finished":
                    continue
                elif self.dispatchers[ii][1] == "unalloc":
                    self.update_instance_ip_list()
                    if ii < len(self.ip_list):
                        profile = self.mdata_machine.copy()
                        profile["hostname"] = self.ip_list[ii]
                        profile["instance_id"] = self.instance_list[ii]
                        disp = [Dispatcher(profile, context_type='ssh-uuid', batch_type='shell', job_record='jr.%.06d.json' % ii), "working"]
                        self.dispatchers[ii] = disp
                        dlog.info(self.ip_list[ii])
                        job_handler = self.dispatchers[ii][0].submit_jobs(resources,
                                                               command,
                                                               work_path,
                                                               self.task_chunks[ii],
                                                               group_size,
                                                               forward_common_files,
                                                               forward_task_files,
                                                               backward_task_files,
                                                               forward_task_deference,
                                                               outlog,
                                                               errlog)
                        self.job_handlers[ii] = job_handler
            if self.check_dispatcher_finished():
                os.remove('apg_id.json')
                self.delete_template()
                self.delete_apg()
                break
            else:
                time.sleep(10)

# status = ["unalloc", "working", "finished", "exception"]
    def check_server(self, dispatcher):
        #dlog.info("check server")
        for ii in range(3):
            try:
                session = SSHSession(dispatcher.remote_profile)
                #dlog.info(True)
                return True
            except:
                pass
        #dlog.info(False)
        return False

    def dispatcher_finish(self, dispatcher, job_handler, mark_failure):
        job_record = job_handler['job_record']
        rjob = job_handler['job_list'][0]
        status = rjob['batch'].check_status()
        if status == JobStatus.terminated :
            machine_status = True
            ssh_active_count = 0
            while True:
                if rjob['context'].ssh_session._check_alive():
                    break
                if not rjob['context'].ssh_session._check_alive():
                    ssh_active_count += 1
                if ssh_active_count == 3:
                    machine_status = False
                    break
        if machine_status == False:
            pass
        else:
            return dispatcher.all_finished(job_handler, mark_failure)

    def check_dispatcher_finished(self):
        for ii in range(len(self.dispatchers)):
            if self.dispatchers[ii][1] == "unalloc" or self.dispatchers[ii][1] == "working":
                return False
        return True
   
    def delete(self, ii):
        request = DeleteInstancesRequest()
        request.set_accept_format('json')
        request.set_InstanceIds([self.instance_list[ii]])
        request.set_Force(True)
        response = self.client.do_action_with_exception(request)

    def make_dispatchers(self):
        dispatchers = []
        if len(self.ip_list) < self.nchunks:
            dlog.info("machine resources unsuffient, %d jobs are running, %d jobs are pending" %(len(self.ip_list), self.nchunks-len(self.ip_list)))
        for ii in range(self.nchunks):
            if ii >= len(self.ip_list):
                disp = [None, "unalloc"]
            else:
                profile = self.mdata_machine.copy()
                profile['hostname'] = self.ip_list[ii]
                profile['instance_id'] = self.instance_list[ii]
                disp = [Dispatcher(profile, context_type='ssh-uuid', batch_type='shell', job_record='jr.%.06d.json' % ii), "working"]
            dispatchers.append(disp)
        return dispatchers

    def delete_machine(self):
        request = DeleteInstancesRequest()
        request.set_accept_format('json')
        if len(self.instance_list) <= 100:
            request.set_InstanceIds(self.instance_list)
            request.set_Force(True)
            response = self.client.do_action_with_exception(request)
        else:
            iteration = len(self.instance_list) // 100
            for i in range(iteration):
                request.set_InstanceIds(self.instance_list[i*100:(i+1)*100])
                request.set_Force(True)
                response = self.client.do_action_with_exception(request)
            if len(self.instance_list) - iteration * 100 != 0:
                request.set_InstanceIds(self.instance_list[iteration*100:])
                request.set_Force(True)
                response = self.client.do_action_with_exception(request)
        self.instance_list = []
        self.ip_list = []
        os.remove('machine_record.json')
        dlog.debug("Successfully free the machine!")

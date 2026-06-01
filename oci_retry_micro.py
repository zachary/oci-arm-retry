import oci
import time
import datetime

# ╔══════════════════════════════════════════════════════╗
# ║  目標：VM.Standard.E2.1.Micro（x86）                 ║
# ║  規格：1 OCPU / 1 GB RAM / 預設 50 GB 磁碟           ║
# ║  架構：x86（比 ARM 好搶）                             ║
# ║  方案：Oracle Always Free（永久免費，每帳號最多 2 台）║
# ║  OS  ：Canonical Ubuntu 22.04                        ║
# ╚══════════════════════════════════════════════════════╝

# ─── 設定 ───────────────────────────────────────────────
COMPARTMENT_ID = (
    "ocid1.compartment.oc1..aaaaaaaauyuqqr4lxvypevhj526u5ickdz6yn73fr5ont6njtiu2o4eh4dwa"  # 改成自己的租用戶 OCID
)
SSH_PUBLIC_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCgf96CyYAM12927Wb+rQxXiSe7gVHa2qXzvCTgllnzQfphn4tXUzGx1adegyxx1u+TygqdhagfphOxmd5a3lMSr3JsDQugbFvwq/nN18a34XDq5DKdssPMYzX6oidZ26MsUg9s0QzRS/bzslNdkl+qH1NCXBX86YV6FsNhVNFkJs2T0fecVgOeeeBZTkJRAwv3F7AwZavJEDCBkeVWqpw4L89zW2Ix6MDc8VFVOhX6HCY/6SDmS8xGfDeBrKEysNmMZmFO1Q7sf5NB3t6AqaKpd18whhSJKzcrx/n5/eyuVp6z0a8QTKfAFLV8o0vMfN+VdQpdVohfmXoUlfLqv6nb"  # 改成自己的 SSH 公鑰（.pub 檔內容）
RETRY_INTERVAL = 90  # 秒
# ────────────────────────────────────────────────────────

# ─── OCI 認證設定 ─────────────────────────────────────────
# 【本機執行】：需在 C:\Users\你的帳號\.oci\config 建立設定檔
#   格式見 README.md，key_file 指向下載的 API 私密金鑰 .pem
#
# 【GitHub Actions 執行】：不需要 config 檔
#   workflow 會從 GitHub Secrets 自動建立，詳見 README.md
# ────────────────────────────────────────────────────────
config = oci.config.from_file()


def get_availability_domain():
    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return ads[0].name


def get_ubuntu_x86_image():
    compute = oci.core.ComputeClient(config)
    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape="VM.Standard.E2.1.Micro",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise Exception("找不到 Ubuntu 22.04 x86 映像檔")
    return images[0].id


def create_vcn_and_subnet():
    network = oci.core.VirtualNetworkClient(config)

    vcns = network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn-micro").data
    if vcns:
        vcn = vcns[0]
        print(f"使用既有 VCN: {vcn.id}")
    else:
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=COMPARTMENT_ID,
                display_name="retry-vcn-micro",
                cidr_block="10.1.0.0/16",
            )
        ).data
        print(f"建立 VCN: {vcn.id}")

        ig = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-ig-micro",
                is_enabled=True,
            )
        ).data

        network.update_route_table(
            vcn.default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        network_entity_id=ig.id,
                    )
                ]
            ),
        )

        security_lists = network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data
        if security_lists:
            existing_egress = security_lists[0].egress_security_rules
            new_ingress = []
            for port in [22, 80, 443, 8501]:
                new_ingress.append(
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port, max=port
                            )
                        ),
                    )
                )
            network.update_security_list(
                security_lists[0].id,
                oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=new_ingress,
                    egress_security_rules=existing_egress,
                ),
            )

    subnets = network.list_subnets(
        COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet-micro"
    ).data
    if subnets:
        subnet = subnets[0]
        print(f"使用既有子網路: {subnet.id}")
    else:
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-subnet-micro",
                cidr_block="10.1.0.0/24",
                prohibit_public_ip_on_vnic=False,
            )
        ).data
        print(f"建立子網路: {subnet.id}")

    return subnet.id


def try_create_instance(subnet_id, ad_name, image_id):
    compute = oci.core.ComputeClient(config)
    instance = compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name="micro-server",
            availability_domain=ad_name,
            shape="VM.Standard.E2.1.Micro",
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data
    return instance


def main():
    print("初始化網路設定...")
    subnet_id = create_vcn_and_subnet()

    print("取得可用性網域...")
    ad_name = get_availability_domain()
    print(f"AD: {ad_name}")

    print("取得 Ubuntu 22.04 x86 映像檔...")
    image_id = get_ubuntu_x86_image()
    print(f"Image ID: {image_id}")

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] 第 {attempt} 次嘗試建立 instance...")

        try:
            instance = try_create_instance(subnet_id, ad_name, image_id)
            print(f"\n✅ 成功！Instance 已建立")
            print(f"   ID: {instance.id}")
            print(f"   狀態: {instance.lifecycle_state}")
            print(f"   請到 Oracle Cloud 主控台查看公用 IP")
            break
        except oci.exceptions.ServiceError as e:
            if "Out of host capacity" in str(e) or "capacity" in str(e).lower():
                print(f"❌ 容量不足，重試...")
            else:
                print(f"❌ API 錯誤: {e.message}，重試...")
        except Exception as e:
            print(f"⚠️ 網路逾時或其他錯誤，重試... ({type(e).__name__})")

        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()

#!/bin/bash

function create_config() {
    local openstack=$1
    local network=$2
    local subnet=$3
    local private_key=$4

    local where=$(dirname $0)

    cat > ~/.teuthology.yaml <<EOF
lock_server: http://localhost:8080/
queue_port: 11300
queue_host: localhost
lab_domain: openstacklocal
canonicalize_hostname: false
teuthology_path: .
results_server:
openstack:
  user-data:
    ubuntu-14.04: teuthology/openstack-ubuntu-user-data.txt
    default: teuthology/openstack-default-user-data.txt
  default-size:
    disk-size: 10
    ram: 1024
    cpus: 1
  default-volumes:
    count: 1
    size: 1
  ssh-key: $private_key
  clusters:
    suse:
      openrc.sh: $where/openrc.sh
      network: $network
      subnet: $subnet
      images: 
        ubuntu-14.04: ubuntu-14.04
        centos-7.0: CentOS-7-x86_64
    the-re:
      openrc.sh: $where/openrc.sh
      server-create: --availability-zone ovh:bm0014.the.re
      volume-create: --availability-zone ovh --type ovh
      network: $network
      subnet: $subnet
      images: 
        ubuntu-14.04: ubuntu-trusty-14.04
        centos-7.0: centos-7
        debian-7.0: debian-wheezy-7.1
    entercloudsuite:
      openrc.sh: $where/openrc.sh
      network: $network
      subnet: $subnet
      images: 
        ubuntu-14.04: GNU/Linux Ubuntu Server 14.04 LTS Trusty Tahr x64
        centos-7.0: GNU/Linux CentOS 7 RAW x64
        debian-7.0: GNU/Linux Debian 7.4 Wheezy x64
EOF
    echo "OVERRIDE ~/.teuthology.yaml with $openstack configuration"
    return 0
}

function teardown_paddles() {
    if pkill -f 'pecan' ; then
	echo "SHUTDOWN the paddles server"
    fi
}

function setup_paddles() {
    local openstack=$1
    local paddles=http://localhost:8080/

    local paddles_dir=$(dirname $0)/../../../../paddles

    if curl --silent $paddles | grep -q paddles  ; then
	echo "OK paddles is running"
	return 0
    fi

    if ! test -d $paddles_dir ; then
	git clone https://github.com/ceph/paddles.git $paddles_dir
    fi

    sudo apt-get -qq install -y sqlite3 beanstalkd

    (
	cd $paddles_dir
	git pull --rebase
	git clean -ffqdx
	perl -p -e "s|^address.*|address = 'http://localhost'|" < config.py.in > config.py
	virtualenv ./virtualenv
	source ./virtualenv/bin/activate
	pip install -r requirements.txt
	pip install sqlalchemy tzlocal requests
	python setup.py develop
	pecan populate config.py
	for id in $(seq 10 30) ; do
	    sqlite3 dev.db "insert into nodes (id,name,machine_type,is_vm,locked,up) values ($id, '${openstack}0$id', 'openstack', 1, 0, 1);"
	done
	pecan serve config.py &
    )
    
    echo "LAUNCHED the paddles server"
}

function get_or_create_keypair() {
    local keypair=$1
    local key_file=$2

    if ! openstack keypair show $keypair > /dev/null 2>&1 ; then
	openstack keypair create $keypair > $key_file || return 1
	chmod 600 $key_file
	echo "CREATED keypair $keypair"
    else
	echo "OK keypair $keypair exists"
    fi
}

function delete_keypair() {
    local keypair=$1
    
    if openstack keypair show $keypair > /dev/null 2>&1 ; then
	openstack keypair delete $keypair || return 1
	echo "REMOVED keypair $keypair"
    fi
}

function setup_dnsmasq() {

    if ! test -f /etc/dnsmasq.d/resolv ; then
	sudo apt-get -qq install -y dnsmasq
	echo resolv-file=/etc/dnsmasq-resolv.conf | sudo tee /etc/dnsmasq.d/resolv
	echo nameserver 8.8.8.8 | sudo tee /etc/dnsmasq-resolv.conf
	sudo /etc/init.d/dnsmasq restart
	sudo sed -ie 's/^#IGNORE_RESOLVCONF=yes/IGNORE_RESOLVCONF=yes/' /etc/default/dnsmasq
	echo "INSTALLED dnsmasq and configured to be a resolver"
    else
	echo "OK dnsmasq installed"
    fi
}

function define_dnsmasq() {
    local subnet=$1
    local openstack=$2
    local prefix=${subnet%.0/24}
    local host_records=/etc/dnsmasq.d/$openstack
    if ! test -f $host_records ; then
	for i in $(seq 1 254) ; do
	    echo host-record=$(printf $openstack%03d $i),$prefix.$i
	done | sudo tee $host_records
	sudo /etc/init.d/dnsmasq restart
	echo "CREATED $host_records"
    else
	echo "OK $host_records exists"
    fi
}

function undefine_dnsmasq() {
    local openstack=$1
    local host_records=/etc/dnsmasq.d/$openstack

    sudo rm -f $host_records
    echo "REMOVED $host_records"
}

function install_packages() {

    if type openstack > /dev/null 2>&1 ; then
	echo "OK openstack command is available"
	return 0
    fi

    if ! test -f /etc/apt/sources.list.d/trusty-backports.list ; then
	echo deb http://archive.ubuntu.com/ubuntu trusty-backports main universe | sudo tee /etc/apt/sources.list.d/trusty-backports.list
	sudo apt-get update
    fi

    sudo apt-get -qq install -y libssl-dev libffi-dev libyaml-dev jq ipcalc libmysqlclient-dev libpython-dev libevent-dev python-tox

    ( cd $(dirname $0)/../../.. ; pip install -r requirements.txt )

    echo "INSTALLED teuthology packages and python requirements"
}

CAT=${CAT:-cat}

function get_network() {
    local network=$1

    local id=$(openstack network list -f json | $CAT | jq '.[] | select(.Name == "'$network'") | .ID')
    eval echo $id
}

function get_or_create_network() {
    local network=$1
    local id=$(get_network $network)
    if test -z "$id" ; then
	id=$(openstack network create -f json $network | $CAT | jq '.[] | select(.Field == "id") | .Value')
	echo "CREATED network $network" >&2
    else
	echo "OK network $network exists" >&2
    fi
    eval echo $id
}

function delete_network() {
    local network=$1
    local id=$(get_network $network)
    if test "$id" ; then 
	neutron net-delete $id || return 1
	echo "REMOVED network $network"
    fi
}

function set_nameserver() {
    local subnet_id=$1
    local nameserver=$2

    eval local current_nameserver=$(neutron subnet-show -f json $subnet_id | jq '.[] | select(.Field == "dns_nameservers") | .Value'    )

    if test "$current_nameserver" = "$nameserver" ; then
	echo "OK nameserver is $nameserver"
    else
	neutron subnet-update --dns-nameserver $nameserver $subnet_id || return 1
	echo "CHANGED nameserver from $current_nameserver to $nameserver"
    fi
}

function get_subnet() {
    local subnet=$1

    local id=$(neutron subnet-list -f json | $CAT | jq '.[] | select(.cidr == "'$subnet'") | .id')
    eval echo $id
}

function get_or_create_subnet() {
    local network_id=$1
    local subnet=$2

    local id=$(get_subnet $subnet)
    if test -z "$id" ; then
	id=$(neutron subnet-create -f json --enable-dhcp $network_id $subnet | grep -v 'Created a new subnet' | $CAT | jq '.[] | select(.Field == "id") | .Value')
	echo "CREATED subnet $subnet" >&2
    else
	echo "OK subnet $subnet exists" >&2
    fi

    eval echo $id
}

function delete_subnet() {
    local subnet=$1
    local id=$(get_subnet $subnet)
    if test "$id" ; then
	neutron port-list -f json | jq '.[] | select(.fixed_ips | contains("'$id'")) | .id' | while read port ; do
	    eval neutron port-update --device-owner clear $port
	    eval neutron port-delete $port
	done
	neutron subnet-delete $id || return 1
	echo "REMOVED subnet $subnet"
    fi
}

function get_or_create_router() {
    local subnet=$1
    local external_network=$2
    local router=$3

    if ! neutron router-show $router >/dev/null 2>&1 ; then
	neutron router-create $router || return 1
	neutron router-interface-add $router $subnet || return 1
	neutron router-gateway-set $router $external_network || return 1
	echo "CREATED router $router"
    else
	echo "OK router $router exists"
    fi
}

function delete_router() {
    local router=$1
    if neutron router-show $router 2>/dev/null ; then
	neutron router-delete $router
	echo "REMOVED router $router"
    fi
}

function verify_openstack() {
    local openrc=$(dirname $0)/openrc.sh
    if ! test -f $openrc ; then
	echo ERROR: download OpenStack credentials in $openrc >&2
	return 1
    fi
    source $openrc
    if ! openstack server list > /dev/null ; then
	echo ERROR: the credentials from $openrc are not working >&2
	return 1
    fi
    echo "OK $OS_TENANT_NAME can use $OS_AUTH_URL"
    return 0
}

function main() {
    local key_file=$(dirname $0)/teuthology.pem
    local network=teuthology-test
    local subnet=10.50.50.0/24
    local nameserver=$(ip a | perl -ne 'print $1 if(/.*inet\s+('${subnet%.0/24}'.\d+)/)')
    local external_network=ovh
    local router=teuthology
    local openstack=the-re

    local do_setup_keypair=false
    local do_create_config=false
    local do_setup_dnsmasq=false
    local do_install_packages=false
    local do_setup_network=false
    local do_setup_paddles=false
    local do_clobber=false

    while [ $# -ge 1 ]; do
        case $1 in
            --verbose)
                set -x
		PS4='${FUNCNAME[0]}: $LINENO: '
                ;;
            --key-file)
                shift
                key_file=$1
                ;;
            --nameserver)
                shift
                nameserver=$1
                ;;
            --network)
                shift
                network=$1
                ;;
            --subnet)
                shift
                subnet=$1
                ;;
            --external-network)
                shift
                external_network=$1
                ;;
            --router)
                shift
                router=$1
                ;;
            --openstack)
                shift
                openstack=$1
                ;;
	    --setup-keypair)
		do_setup_keypair=true
		;;
            --config)
                do_create_config=true
                ;;
            --setup-dnsmasq)
		do_setup_dnsmasq=true
                ;;
            --install)
                do_install_packages=true
                ;;
            --setup-network)
		do_setup_network=true
                ;;
            --paddles)
                do_setup_paddles=true
                ;;
	    --setup-all)
		do_setup_keypair=true
                do_create_config=true
		do_setup_dnsmasq=true
                do_install_packages=true
		do_setup_network=true
                do_setup_paddles=true
		;;
	    --clobber)
		do_clobber=true
		;;
            *)
                echo $1 is not a known option
                return 1
                ;;
        esac
	shift
    done

    if $do_install_packages ; then
	install_packages || return 1
    fi

    verify_openstack || return 1

    if $do_create_config ; then
	create_config $openstack $network $subnet $key_file || return 1
    fi

    if $do_setup_network ; then
        local network_id=$(get_or_create_network $network)
        local subnet_id=$(get_or_create_subnet $network_id $subnet)
        get_or_create_router $subnet_id $external_network $router || return 1
	set_nameserver $subnet_id $nameserver
    fi

    if $do_setup_keypair ; then
	get_or_create_keypair teuthology $key_file || return 1
    fi

    if $do_setup_dnsmasq ; then
        setup_dnsmasq || return 1
        define_dnsmasq $subnet $openstack || return 1
    fi

    if $do_setup_paddles ; then
	setup_paddles $openstack || return 1
    fi

    if $do_clobber ; then
	delete_subnet $subnet
	delete_router $router
	delete_network $network
	undefine_dnsmasq $openstack || return 1
	delete_keypair teuthology || return 1
	teardown_paddles
    fi
}

main "$@"

# bash teuthology/test/integration/setup-openstack.sh --openstack entercloudsuite --key-file ~/.ssh/id_rsa --external-network PublicNetwork --nameserver 10.50.50.2 --setup-all

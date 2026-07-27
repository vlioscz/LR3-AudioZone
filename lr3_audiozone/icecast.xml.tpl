<icecast>
    <location>Home Assistant</location>
    <admin>admin@localhost</admin>
    <hostname>%%HOSTNAME%%</hostname>

    <limits>
        <clients>50</clients>
        <sources>10</sources>
        <queue-size>524288</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-size>16384</burst-size>
    </limits>

    <authentication>
        <source-password>%%SOURCE_PASSWORD%%</source-password>
        <relay-password>%%SOURCE_PASSWORD%%</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>%%SOURCE_PASSWORD%%</admin-password>
    </authentication>

    <listen-socket>
        <port>%%PORT%%</port>
    </listen-socket>

    <fileserve>1</fileserve>

    <paths>
        <basedir>/usr/share/icecast2</basedir>
        <logdir>/var/log/icecast2</logdir>
        <webroot>/usr/share/icecast2/web</webroot>
        <adminroot>/usr/share/icecast2/admin</adminroot>
        <alias source="/" destination="/status.xsl"/>
    </paths>

    <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>3</loglevel>
        <logsize>10000</logsize>
    </logging>

    <security>
        <chroot>0</chroot>
        <changeowner>
            <user>icecast2</user>
            <group>icecast</group>
        </changeowner>
    </security>
</icecast>
